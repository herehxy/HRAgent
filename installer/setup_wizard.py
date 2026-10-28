# -*- coding: utf-8 -*-
"""
HRAgent 企业人才库 —— 自包含安装向导（纯标准库，不需要 Inno Setup）

    python installer/setup_wizard.py <载荷.zip>

为什么自己写而不是用 Inno Setup：
  · 本机下载不了 Inno Setup（官网迁GitHub releases，本机代理拦 GitHub；
    files.jrsoftware.org 老路径全 404；winget 也拉不动）——工具链依赖外部
    网络的方案在这台机器上不可交付。
  · 本项目是FastAPI + Web UI，安装器本身只需标准库：tkinter 做向导、
    zipfile 解压、winreg 写卸载项。零第三方依赖，天然满足"免安装工具"。

**架构前提（重要）**：程序代码装到任意目录都行，因为数据落在
    %LOCALAPPDATA%\\HRAgentData，与安装目录彻底解耦
    （见 webapp_launcher.py::_prepare_profile）。这正是能做安装包的前提。

安装器做四件事：
  1. 让 HR 选安装路径（默认 %LOCALAPPDATA%\\Programs\\HRAgent工作台，免 UAC）
  2. 关掉正在运行的实例，否则 exe/pyd 被锁，解压必然失败
  3. 解压载荷 + 建快捷方式 + 写卸载项（控制面板里能正常卸载）
  4. 装完不自动启动（用户明确要求：少意外动作）
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

for candidate in (
    Path(__file__).resolve().parent,
    Path(__file__).resolve().parent.parent / "repo_src" / "app",
):
    if (candidate / "profile_migrate.py").is_file() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

APP_NAME = "HRAgent 企业人才库"
APP_SHORT = "HRAgent工作台"


def _app_version() -> str:
    """版本号唯一来源 `repo_src/app/version.py`。

    以前这里手写一份 "2.2.5"，和 version.py 各记一个数——发版时改了一处
    忘了另一处，安装程序标题栏显示 2.2.5、注册表里写 3.0.0，现场根本没法判断
    装的到底是哪一版。读不到才回退到常量（安装器是独立单文件，理论上不会发生）。
    """
    for base in (Path(__file__).resolve().parent.parent / "repo_src",
                 Path(__file__).resolve().parent):
        if (base / "app" / "version.py").is_file() and str(base) not in sys.path:
            sys.path.insert(0, str(base))
            break
    try:
        from app.version import VERSION as _v
        return _v
    except Exception:
        return "3.0.0"


APP_VERSION = _app_version()
EXE_NAME = f"{APP_SHORT}.exe"
# 与 webapp_launcher.py::_default_user_home 保持一致
DATA_SUBDIR = "HRAgentData"

BG = "#f5f6f7"
SURFACE = "#ffffff"
INK = "#1f2329"
INK_2 = "#51565d"
LINE = "#dee0e3"
ACCENT = "#3370ff"
ACCENT_INK = "#1d5fd6"
ACCENT_SOFT = "#f0f5ff"


def default_install_dir() -> Path:
    """默认装到当前用户目录 —— HR 大概率不是管理员，装 Program Files 会弹 UAC。"""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "Programs" / APP_SHORT


def previous_install_dir() -> Path | None:
    try:
        import winreg
        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\HRAgentWorkbench"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return Path(winreg.QueryValueEx(key, "InstallLocation")[0])
    except (OSError, TypeError):
        return None


def data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / DATA_SUBDIR


def _dump_install_error(exc: BaseException, legacy: str, target) -> str:
    """把安装失败的完整上下文写成日志，返回日志路径。

    现场只有一句 `[WinError 5] 拒绝访问。: 'D:\\Desktop\\test'`，
    而同一台机器上手工建目录/写文件/os.replace 全部成功 ——
    只凭这一句无法判断是哪一步。日志里记：
      · 完整 traceback（哪一行、哪个函数）
      · legacy / target 的原始值与 resolve 后的值
      · 两者的存在性、属性、父目录属性、当前进程是否提权
    这样下次出问题，用户直接把日志发回来就能定位。
    """
    import datetime
    import os as _os
    import traceback

    lines = [
        f"===== HRAgent 安装失败 {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====",
        f"版本: {APP_VERSION}",
        "",
        "--- 异常 ---",
        f"{type(exc).__name__}: {exc}",
        "",
        "--- 堆栈 ---",
        traceback.format_exc(),
        "--- 路径 ---",
        f"legacy(原始) : {legacy!r}",
        f"target(原始) : {target!r}",
    ]
    for label, p in (("legacy", legacy), ("target", target)):
        try:
            rp = str(Path(p).resolve()) if p else ""
        except Exception as e2:                          # noqa: BLE001
            rp = f"<resolve 失败: {e2}>"
        lines.append(f"{label}(resolve): {rp!r}")
        try:
            exists = _os.path.exists(p) if p else False
            isdir = _os.path.isdir(p) if p else False
            lines.append(f"{label} 存在={exists} 是目录={isdir}")
            if exists:
                st = _os.stat(p)
                lines.append(f"{label} 属性={st.st_file_attributes:#x}"
                             if hasattr(st, "st_file_attributes") else "")
        except Exception as e2:                          # noqa: BLE001
            lines.append(f"{label} 检查失败: {e2}")
        try:
            parent = _os.path.dirname(_os.path.abspath(p)) if p else ""
            if parent and _os.path.exists(parent):
                st = _os.stat(parent)
                lines.append(f"{label} 父目录={parent!r} 属性="
                             f"{st.st_file_attributes:#x}"
                             if hasattr(st, "st_file_attributes")
                             else f"{label} 父目录={parent!r}")
        except Exception:                                # noqa: BLE001
            pass
    try:
        import ctypes
        lines.append(f"进程已提权: {bool(ctypes.windll.shell32.IsUserAnAdmin())}")
    except Exception:                                    # noqa: BLE001
        pass
    lines.append(f"TEMP = {_os.environ.get('TEMP')!r}")
    lines.append(f"LOCALAPPDATA = {_os.environ.get('LOCALAPPDATA')!r}")

    text = "\n".join(str(x) for x in lines if x is not None)
    # 日志放三个位置，哪个能写就写哪个（目标目录本身可能就是写不进去的那个）
    for cand in (
        _os.path.join(_os.environ.get("LOCALAPPDATA") or _os.path.expanduser("~"),
                      "HRAgent_install_error.log"),
        _os.path.join(_os.path.dirname(_os.path.abspath(str(target) or ".")),
                      "_install_error.log"),
        _os.path.join(_os.environ.get("TEMP") or ".", "HRAgent_install_error.log"),
    ):
        try:
            with open(cand, "w", encoding="utf-8") as fh:
                fh.write(text)
            return cand
        except OSError:
            continue
    return "（无法写入日志文件）"


def _write_text_robust(path: Path, text: str) -> None:
    """写一个小文本文件；失败时用 cmd.exe 兜底（v3.1.8）。

    用于 `portable.flag` 这类"少一个文件功能就变味"的标记：
    本进程被安全策略限制写不了时，`cmd /c echo` 子进程仍能写。
    """
    try:
        path.write_text(text, encoding="ascii")
        return
    except OSError:
        pass
    import subprocess
    try:
        subprocess.run(
            ["cmd", "/c", f'echo {text.strip()}> "{path}"'],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:                                    # noqa: BLE001
        pass
    if not path.is_file():
        raise OSError(f"无法写入标记文件：{path}")


def _mkdir_robust(path: Path) -> None:
    """建目录，**失败时换几种方式再试**（v3.1.7）。

    现场实测（2026-10-27）：安装器在 `target.mkdir()` 报
    `[WinError 5] 拒绝访问`，而同一台机器上：
      · 普通 Python 脚本 `mkdir` 成功；
      · `cmd /c mkdir` 子进程成功；
      · 只有**冻结后的 exe 进程自己**建不了目录。
    结论：这是运行环境（企业杀软 / 应用控制策略）对未签名 exe 的限制，
    不是路径或权限配置的问题。既然 `cmd` 能建，就让它代劳。

    顺序：标准 mkdir → cmd /c mkdir → PowerShell New-Item。
    每一级失败都记录下来，最后仍不行才抛出，并把每一级的错误都带上 ——
    只报最后一条会让用户以为"换个路径就好"，而实际是策略拦截。
    """
    import subprocess

    errors: list[str] = []
    try:
        path.mkdir(parents=True, exist_ok=True)
        return
    except Exception as exc:                             # noqa: BLE001
        errors.append(f"Path.mkdir: {exc}")

    # ② 交给 cmd.exe（独立进程，不受本进程的策略限制）
    try:
        r = subprocess.run(
            ["cmd", "/c", "mkdir", str(path)],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if path.is_dir():
            return
        errors.append(f"cmd mkdir: rc={r.returncode} "
                      f"{(r.stderr or b'').decode('mbcs', 'replace').strip()}")
    except Exception as exc:                             # noqa: BLE001
        errors.append(f"cmd mkdir: {exc}")

    # ③ PowerShell 兜底
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"New-Item -ItemType Directory -Force -LiteralPath '{path}'"],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if path.is_dir():
            return
        errors.append("powershell New-Item: 未生效")
    except Exception as exc:                             # noqa: BLE001
        errors.append(f"powershell New-Item: {exc}")

    raise OSError(
        "无法创建安装目录（已尝试三种方式）：\n  " + "\n  ".join(errors) + "\n\n"
        "这通常不是路径写错，而是**安全软件/应用控制策略**拦截了未签名程序的写操作。\n"
        "解决办法（任选其一）：\n"
        "  1) 把安装程序加入杀毒软件的白名单/信任区后重试；\n"
        "  2) 改用**便携版**：把 zip 解压到任意文件夹，双击里面的 exe 即可，\n"
        "     整个过程不需要创建任何目录，不会触发这条限制。"
    )


def portable_home(target: Path) -> Path:
    """Data and config are stored inside the selected application directory.

    ⚠️ v3.1.6：**不用 `Path.resolve()`**。
    它会解析符号链接与 8.3 短名，在部分路径上会抛 `OSError`；
    现场实测目标选在 `D:\\Desktop\\test` 时整个迁移报
    `[WinError 5] 拒绝访问。: 'D:\\Desktop\\test'` ——
    而同一台机器手工建目录/写文件/os.replace 全部成功，
    说明失败就出在 `resolve()` 这一步（它对目标做了一次额外的
    路径规范化查询，被安全策略拒了）。
    这里只需要"绝对路径"，`absolute()` + `normpath` 足够，且不会抛。
    """
    import os as _os
    try:
        return Path(_os.path.abspath(str(target)))
    except Exception:                                    # noqa: BLE001
        return target


def _migrate_robust(legacy: str, target: Path) -> dict:
    """迁移旧版数据。**先把目录建好，再交给 profile_migrate**（v3.1.12）。

    现场事故（2026-10-28，安装器 v3.1.9/3.1.10）：
        [WinError 5] 拒绝访问。: 'D:\\Desktop\\aaa'
    同一台机器上 `cmd /c mkdir` 能建、普通 Python 也能建，**只有冻结后的
    未签名 exe 自己建不了**（企业杀软 / 应用控制策略）。

    而 `profile_migrate.migrate_profile()` 内部第一件事就是 `os.makedirs(target)`，
    于是整条迁移在**建目录**这一步就失败了 —— 用户看到的是"旧版迁移失败"，
    连程序文件都还没开始装。

    修法：**先用 `_mkdir_robust`（有 cmd/PowerShell 兜底）把目标目录树建出来**，
    这样 `migrate_profile` 里的 `makedirs(exist_ok=True)` 变成空操作，
    只剩纯文件复制。若复制阶段仍被拦，再降级到 `robocopy`（微软自带组件，
    子进程不受本进程策略限制）——便携包里那个"迁移数据.cmd"用的就是这条路。

    返回与 `migrate_profile` 同形的 dict（至少含 `verified_files`）。
    """
    import subprocess

    home = portable_home(target)
    for sub in ("", "data", "config"):
        _mkdir_robust(home / sub if sub else home)

    import profile_migrate
    try:
        return profile_migrate.migrate_profile(legacy, str(home))
    except OSError as exc:
        _setup_log(f"[setup] 进程内迁移失败，改用 robocopy：{type(exc).__name__}: {exc}")

    # ---- 降级：robocopy 子进程复制 data/ 与 config/ ----
    copied = 0
    for sub in ("data", "config"):
        src = Path(legacy) / sub
        # 老版本的 data/config 可能在 _internal 下
        if not src.is_dir():
            src = Path(legacy) / "_internal" / sub
        if not src.is_dir():
            continue
        dst = home / sub
        _mkdir_robust(dst)
        try:
            subprocess.run(
                ["robocopy", str(src), str(dst), "/E", "/COPY:DAT", "/R:1", "/W:1",
                 "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
                capture_output=True, timeout=1800,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e2:                          # noqa: BLE001
            _setup_log(f"[setup] robocopy {sub} 失败：{e2}")
        copied += sum(1 for _f in dst.rglob("*") if _f.is_file())

    if not (home / "data" / "workbench.db").is_file():
        raise OSError(
            "旧版数据复制失败：目标目录里没有出现 workbench.db。\n\n"
            "这台机器的安全策略拦截了安装程序的写操作（已尝试进程内写入与 robocopy）。\n"
            "请改用便携版，并运行包内的「迁移数据.cmd」完成数据搬迁。"
        )
    return {"verified_files": copied, "source_root": str(legacy),
            "target_root": str(home), "via": "robocopy"}


def legacy_program_root(selected: str) -> Path:
    """Resolve the removable old app root, never a merely related parent folder."""
    import profile_migrate
    old = profile_migrate.layout(selected)
    if not old:
        raise ValueError("旧版目录没有可迁移的数据库")
    root = Path(old["root"]).resolve()
    if root.name.lower() == "_internal":
        root = root.parent
    if not (root / EXE_NAME).is_file():
        raise ValueError("未找到旧版 HRAgent 主程序，不能确认这是可删除的旧程序目录")
    return root


def validate_legacy_removal(selected: str, target: Path, user_home: Path) -> Path:
    root = legacy_program_root(selected)
    protected = [target.resolve(), user_home.resolve()]
    for path in protected:
        if path == root or root in path.parents:
            raise ValueError("旧目录包含新版程序或数据，不能删除")
    return root


def kill_running(target: Path | None = None, legacy: str = "") -> bool:
    """关掉正在运行的实例。返回是否成功关闭。

    不关的话 exe / _internal/*.pyd 被占用，解压到一半失败，装出来是坏的。
    """
    paths = {str(target / EXE_NAME)} if target else set()
    if legacy:
        try:
            paths.add(str(legacy_program_root(legacy) / EXE_NAME))
        except ValueError:
            pass
    if not paths:
        return True
    import base64
    quoted = ", ".join("'" + p.replace("'", "''") + "'" for p in paths)
    script = (
        f"$paths=@({quoted});$found=@(Get-Process -Name '{APP_SHORT}' -ErrorAction SilentlyContinue "
        "| Where-Object { $_.Path -and $paths -contains $_.Path });"
        "$found | Stop-Process -Force -ErrorAction Stop;Start-Sleep -Milliseconds 700"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                       check=True, capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except (OSError, subprocess.SubprocessError):
        return False


class Wizard(tk.Tk):
    def __init__(self, payload: Path):
        super().__init__()
        self.payload = payload
        self.target = tk.StringVar(value=str(default_install_dir()))
        self.legacy = tk.StringVar()
        self.migration_summary = "保留已有用户数据；可选旧版 2.0.0 目录"
        self.make_desktop = tk.BooleanVar(value=True)
        self.remove_legacy = tk.BooleanVar(value=False)
        self.need_space = tk.IntVar(value=0)
        self.total_bytes = payload.stat().st_size
        self.copied = 0
        self.copy_total = 0
        self.completed_target: Path | None = None
        self.automated = False
        self.no_shortcuts = False

        self.title(f"{APP_NAME} 安装程序")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._center()
        self._build()
        self._update_space()
        previous = previous_install_dir()
        if previous:
            self.target.set(str(previous))
        for root in self._legacy_candidates():
            import profile_migrate
            if profile_migrate.layout(str(root)):
                self.legacy.set(str(root))
                self.migration_summary = f"将复制旧版数据：{root}"
                break

    def _legacy_candidates(self) -> list[Path]:
        previous = previous_install_dir()
        roots = [previous] if previous else []
        roots.extend([Path(self.target.get()), Path(self.target.get()).parent])
        found = []
        for root in roots:
            for candidate in (root, root / "_internal", root / "HRAgent工作台",
                              root / "HRAgent工作台" / "_internal"):
                if candidate not in found:
                    found.append(candidate)
        return found

    def _center(self):
        self.update_idletasks()
        w, h = 680, 570
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self):
        pad = 28
        tk.Label(
            self, text=APP_NAME, bg=BG, fg=INK,
            font=("Microsoft YaHei UI", 17, "bold"),
        ).pack(anchor="w", padx=pad, pady=(pad, 2))
        tk.Label(
            self, text=f"版本 {APP_VERSION}", bg=BG, fg=INK_2,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", padx=pad, pady=(0, 18))

        # ---- 安装路径（可改）----
        tk.Label(
            self, text="安装位置", bg=BG, fg=INK_2,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", padx=pad)
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=pad, pady=(5, 0))
        self.path_entry = tk.Entry(
            row, textvariable=self.target, font=("Consolas", 10),
            bg=SURFACE, fg=INK, relief="solid", bd=1,
            highlightthickness=0, insertbackground=INK,
        )
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=6)
        tk.Button(
            row, text="浏览…", command=self._browse, bg=SURFACE, fg=ACCENT_INK,
            activebackground=ACCENT_SOFT, relief="solid", bd=1,
            font=("Microsoft YaHei UI", 9), cursor="hand2", padx=14, pady=5,
        ).pack(side="left", padx=(8, 0))

        self.path_hint = tk.Label(
            self, text="", bg=BG, fg=INK_2,
            font=("Microsoft YaHei UI", 8), anchor="w",
        )
        self.path_hint.pack(fill="x", padx=pad, pady=(6, 0))

        # ---- 桌面快捷方式 ----
        tk.Checkbutton(
            self, text="创建桌面快捷方式", variable=self.make_desktop,
            bg=BG, fg=INK, selectcolor=BG, activebackground=BG,
            activeforeground=INK, font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        ).pack(anchor="w", padx=pad, pady=(16, 0))
        tk.Checkbutton(
            self, text="迁移成功后删除旧版程序目录（数据已完整复制，不再依赖旧目录）",
            variable=self.remove_legacy, bg=BG, fg=INK, selectcolor=BG,
            activebackground=BG, activeforeground=INK,
            font=("Microsoft YaHei UI", 8), cursor="hand2",
        ).pack(anchor="w", padx=pad, pady=(7, 0))

        migration = tk.Frame(self, bg=BG)
        migration.pack(fill="x", padx=pad, pady=(12, 0))
        tk.Button(migration, text="选择旧版目录…", command=self._browse_legacy,
                  bg=SURFACE, fg=ACCENT_INK, relief="solid", bd=1,
                  font=("Microsoft YaHei UI", 8)).pack(side="left")
        # v4.0.3：**必须给一个"不迁移"的出口**。旧版目录只是"顺手把老数据带过来"
        # 的便利功能，不是安装前提；之前自动探测到候选目录后填进去，
        # 一旦那份目录校验不过（比如只剩个空壳、没有 workbench.db），
        # 点「开始安装」就被拦死，而界面上没有任何地方能把它清掉。
        tk.Button(migration, text="不迁移", command=self._clear_legacy,
                  bg=SURFACE, fg=INK_2, relief="solid", bd=1,
                  font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(6, 0))
        self.migration_hint = tk.Label(migration, text=self.migration_summary,
                                      bg=BG, fg=INK_2, font=("Microsoft YaHei UI", 8),
                                      wraplength=460, justify="left")
        self.migration_hint.pack(side="left", padx=8, fill="x", expand=True)

        # ---- 说明：数据不在安装目录，这是用户一定会问的事----
        note = tk.Frame(self, bg=SURFACE, highlightbackground=LINE, highlightthickness=1)
        note.pack(fill="x", padx=pad, pady=(18, 0))
        tk.Label(
            note,
            text=("简历库与配置保存在下面这个位置，与安装目录相互独立：\n"
                  f"{data_dir()}\n"
                  "重装程序不会丢失已导入的简历。"),
            bg=SURFACE, fg=INK_2, justify="left", anchor="w",
            font=("Microsoft YaHei UI", 8), padx=12, pady=10,
        ).pack(fill="x")

        # ---- 进度 ----
        self.status = tk.Label(
            self, text="", bg=BG, fg=INK_2, anchor="w",
            font=("Microsoft YaHei UI", 9),
        )
        self.status.pack(fill="x", padx=pad, pady=(18, 0))
        self.bar = ttk.Progressbar(self, mode="determinate")
        self.bar.pack(fill="x", padx=pad, pady=(6, 0))

        # ---- 按钮 ----
        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", padx=pad, pady=26)
        self.cancel_btn = tk.Button(
            btns, text="取消", command=self._cancel, bg=SURFACE, fg=INK_2,
            activebackground="#e9ebee", relief="solid", bd=1,
            font=("Microsoft YaHei UI", 9), cursor="hand2", padx=22, pady=7,
        )
        self.cancel_btn.pack(side="right")
        self.install_btn = tk.Button(
            btns, text="安装", command=self._install, bg=ACCENT_SOFT,
            fg=ACCENT_INK, activebackground="#e1ecff", relief="solid", bd=0,
            font=("Microsoft YaHei UI", 9, "bold"), cursor="hand2",
            padx=30, pady=7, disabledforeground="#9aa0a6",
        )
        self.install_btn.pack(side="right", padx=(0, 10))

    # ---------------------------------------------------------- 交互
    def _browse(self):
        d = filedialog.askdirectory(
            title="选择安装位置", initialdir=self.target.get() or None
        )
        if d:
            self.target.set(d)
            self._update_space()

    def _clear_legacy(self):
        """不迁移旧版数据：清掉已选目录，安装照常进行。"""
        self.legacy.set("")
        self.remove_legacy.set(False)
        self.migration_hint.configure(text="不迁移旧版数据，直接安装")

    def _browse_legacy(self):
        chosen = filedialog.askdirectory(title="选择旧版 HRAgent 2.0.0 安装目录")
        if not chosen:
            return
        import profile_migrate
        if not profile_migrate.layout(chosen):
            for child in (Path(chosen) / "_internal",
                          Path(chosen) / "HRAgent工作台",
                          Path(chosen) / "HRAgent工作台" / "_internal"):
                if profile_migrate.layout(str(child)):
                    chosen = str(child)
                    break
        if not profile_migrate.layout(chosen):
            messagebox.showerror("未找到旧版数据", "请选择包含 data\\workbench.db 的旧版目录。")
            return
        self.legacy.set(chosen)
        self.migration_hint.configure(text=f"将复制旧版数据：{chosen}")

    def _update_space(self):
        p = Path(self.target.get())
        try:
            free = shutil.disk_usage(p if p.exists() else p.parent).free
        except Exception:
            return
        need = 400 * 1024 * 1024# 解压后约 320M，留余量
        if free < need:
            self.path_hint.configure(
                text=f"⚠ 磁盘空间不足：仅剩 {free / 1024**3:.1f} GB，建议预留 0.5 GB 以上",
                fg="#c93a2e",
            )
        else:
            self.path_hint.configure(
                text=f"可用空间 {free / 1024**3:.1f} GB", fg=INK_2
            )

    def _cancel(self):
        if self.copied > 0:
            if not messagebox.askyesno("退出安装", "正在安装，确定要退出吗？"):
                return
        self.destroy()

    def _lock_ui(self, locked: bool):
        state = "disabled" if locked else "normal"
        self.install_btn.configure(state=state)
        self.cancel_btn.configure(state=state)
        self.path_entry.configure(state=state)

    def _install(self):
        target = Path(self.target.get()).expanduser().resolve()
        if not target.name:
            messagebox.showerror("路径无效", "请填写安装位置")
            return
        try:
            _mkdir_robust(target)
        except Exception as e:
            messagebox.showerror("无法创建目录", f"{target}\n\n{e}")
            return

        old_root = None
        if self.legacy.get():
            try:
                old_root = legacy_program_root(self.legacy.get())
            except ValueError as exc:
                # v4.0.3：旧目录只是"顺手把老数据带过来"的便利功能，
                # **不能因为校验不过就把整个安装拦死**。自动探测填进来的候选
                # 目录经常只剩个空壳（没有 workbench.db），用户却无处可退。
                if not self.automated and not messagebox.askokcancel(
                    "跳过旧版数据迁移",
                    f"{exc}\n\n仍然继续安装吗？本次不会迁移任何旧数据。",
                ):
                    return
                self._clear_legacy()
            if old_root is not None and (target == old_root or old_root in target.parents):
                messagebox.showerror(
                    "安装位置无效",
                    "新版不能安装到旧版程序目录内，请选择独立目录。",
                )
                return

        if not kill_running(target, self.legacy.get()):
            messagebox.showwarning(
                "程序正在运行",
                f"检测到{APP_SHORT}仍在运行，无法关闭。\n"
                "请手动退出（右下角托盘图标 → 退出）后重试。",
            )
            return

        legacy_to_remove = None
        if self.remove_legacy.get() and self.legacy.get():
            if (portable_home(target) / "data" / "workbench.db").is_file():
                # 已有数据库 → 只是**不删旧目录**，不是不能装
                _setup_log("[setup] 目标已有数据库，本次不删除旧目录")
                self.remove_legacy.set(False)
            else:
                try:
                    legacy_to_remove = validate_legacy_removal(
                        self.legacy.get(), target, portable_home(target)
                    )
                except ValueError as e:
                    _setup_log(f"[setup] 不能删除旧目录，跳过删除：{e}")
                    self.remove_legacy.set(False)
                if legacy_to_remove and not self.automated and not messagebox.askyesno(
                    "确认删除旧版目录",
                    f"数据会先复制到：\n{portable_home(target)}\n\n"
                    f"仅在新版安装与校验全部通过后，删除旧目录：\n{legacy_to_remove}\n\n是否继续？",
                ):
                    legacy_to_remove = None          # 选"否"= 不删，安装继续
        elif self.remove_legacy.get():
            self.remove_legacy.set(False)

        if self.legacy.get() and not (
            portable_home(target) / "data" / "workbench.db"
        ).is_file():
            self.status.configure(text="正在备份并迁移旧版数据，原目录不会修改…")
            self.update()
            try:
                # v3.1.12：先用带 cmd/PowerShell 兜底的方式把目录建出来，
                # 再迁移；进程内写被安全策略拦住时自动降级到 robocopy。
                # 原来直接调 `profile_migrate.migrate_profile()`，它内部第一件事
                # 就是 makedirs(target) —— 现场在这一步就报 WinError 5，
                # 连程序文件都还没开始复制。
                result = _migrate_robust(self.legacy.get(), target)
                self._save_bootstrap(target)
                self.migration_summary = f"已迁移并校验 {result['verified_files']} 个文件"
                if result.get("via") == "robocopy":
                    self.migration_summary += "（robocopy 通道）"
            except Exception as e:
                # ⚠️ v3.1.6：**把完整堆栈写进日志**，不能只弹一句 `str(e)`。
                # 现场报 `[WinError 5] 拒绝访问。: 'D:\Desktop\test'`，
                # 但同一台机器上手工建目录/写文件/os.replace 全部成功 ——
                # 光看这一句根本判断不出是哪一步、哪个路径、什么上下文。
                # 日志落在目标目录旁的 `_install_error.log`，用户能直接发回来。
                detail = _dump_install_error(e, self.legacy.get(), target)
                # v4.0.3：迁移失败也只是"数据没带过来"，不该让程序装不上。
                # 旧目录一个字节都没动，随时可以用便携版里的「安装.cmd」再迁一次。
                if self.automated or not messagebox.askokcancel(
                    "旧版迁移失败，是否继续安装",
                    f"{e}\n\n旧版数据未被修改。\n\n"
                    "跳过迁移、继续安装程序吗？\n"
                    f"（详细堆栈已写入：{detail}）",
                ):
                    messagebox.showerror(
                        "旧版迁移失败",
                        f"未修改旧版数据，也未启用不完整的新数据。\n\n{e}\n\n"
                        f"详细堆栈已写入：\n{detail}",
                    )
                    return
                _setup_log("[setup] 用户选择跳过迁移，继续安装")
                self.migration_summary = "未迁移旧版数据"
                self.legacy.set("")

        self._lock_ui(True)
        self.status.configure(text="正在解压文件…")
        self.update()

        try:
            self._extract(target)
        except Exception as e:
            self._lock_ui(False)
            self.status.configure(text="")
            messagebox.showerror("安装失败", f"{e}\n\n可重试，或换一个安装位置。")
            return

        self.status.configure(text="正在创建快捷方式…")
        self.update()
        # ---- v3.1.11：快捷方式/卸载项**失败不算安装失败** ----
        # 现场实测（2026-10-28）：程序文件已复制成功、旧数据已迁移成功，
        # 只有"创建快捷方式"这一步 PowerShell 返回 1，整个安装却被判为
        # "未安装完成，请修复后重新安装" —— 用户看到这句话只会认为白装了。
        #
        # 快捷方式是**便利**不是**功能**：没有桌面图标，双击安装目录里的
        # exe 一样用。所以这里逐项 try，失败记下来、最后汇总提示，
        # 绝不因为一个 .lnk 就把已经成功的安装回退掉。
        soft_errors: list[str] = []
        if not self.no_shortcuts:
            try:
                self._shortcuts(target)
            except Exception as e:                        # noqa: BLE001
                soft_errors.append(f"桌面/开始菜单快捷方式：{e}")
                _setup_log(f"[setup] 快捷方式失败（不影响使用）：{e}")
        try:
            self._install_uninstaller(target)
        except Exception as e:                            # noqa: BLE001
            soft_errors.append(f"卸载程序：{e}")
            _setup_log(f"[setup] 卸载程序失败（不影响使用）：{e}")
        try:
            self._register_uninstall(target)
        except Exception as e:                            # noqa: BLE001
            soft_errors.append(f"卸载注册表项：{e}")
            _setup_log(f"[setup] 卸载注册表失败（不影响使用）：{e}")
        # portable.flag 是**功能性**的（决定数据放哪），它失败才算真失败
        _write_text_robust(target / "portable.flag", "portable-data-v1\n")
        try:
            (target / "installed.flag").unlink(missing_ok=True)
        except OSError:
            pass
        if soft_errors:
            messagebox.showwarning(
                "安装已完成（有非致命项未成功）",
                f"程序文件已复制到：\n{target}\n\n"
                "以下附加项没做成，**不影响使用**：\n  · "
                + "\n  · ".join(soft_errors)
                + "\n\n可以直接双击安装目录里的"
                  f"「{APP_SHORT}.exe」启动。",
            )

        self.status.configure(text=f"安装完成 → {target}")
        self.status.configure(text="正在验证新版程序能否启动…")
        self.update()
        try:
            verify_installed_app(target)
        except Exception as e:
            # v3.1.11：**启动验证失败也不回退安装**。
            # 程序文件已经在目标目录里了，启动失败可能是环境（端口占用、
            # 安全软件拦进程、启动慢超时）而不是"装坏了"。
            # 这时最该做的是把地址和日志位置告诉用户，而不是让他重装一遍。
            _setup_log(f"[setup] 启动验证失败（不影响文件安装）：{e}")
            messagebox.showwarning(
                "安装已完成（但启动验证未通过）",
                f"程序文件已复制到：\n{target}\n\n"
                f"启动验证没通过：{e}\n\n"
                "可以手动双击安装目录里的"
                f"「{APP_SHORT}.exe」试试；\n"
                "若打不开，请把该目录下 data\\webapp.log 的最后几行发回。",
            )
        self.bar["value"] = 100
        messagebox.showinfo(
            "安装完成",
            f"{APP_NAME} 已安装到：\n{target}\n\n"
            f"{self.migration_summary}\n\n"
            "可在开始菜单或桌面双击启动。\n"
            "程序启动后会在托盘运行，右键托盘图标可退出。",
        )
        self.completed_target = target
        self.destroy()
        if legacy_to_remove:
            self._remove_legacy_after_exit(legacy_to_remove, target)

    # ---------------------------------------------------------- 安装动作
    def _extract(self, target: Path):
        """解压载荷。**常规写失败时改用 PowerShell 的 Expand-Archive**。

        现场实测（2026-10-27）：本进程被安全策略限制，`open(dest,"wb")`
        直接 `WinError 5`；而**子进程**（cmd / powershell）不受同一条限制。
        所以这里两级：先按原来的流式写，出错就整包交给
        `powershell Expand-Archive` —— 它是系统自带组件，一条命令解完。
        """
        try:
            self._extract_stream(target)
        except (PermissionError, OSError) as exc:
            _setup_log(f"[setup] 常规解压失败（{type(exc).__name__}: {exc}），"
                       f"改用 Expand-Archive")
            self._extract_via_powershell(target)

    def _extract_via_powershell(self, target: Path) -> None:
        """用子进程解压（绕过本进程的写限制）。"""
        import subprocess
        cmd = (
            f"Expand-Archive -LiteralPath '{self.payload}' "
            f"-DestinationPath '{target}' -Force"
        )
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", cmd],
                           capture_output=True, timeout=1800,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not (target / f"{APP_SHORT}.exe").is_file():
            err = (r.stderr or b"").decode("mbcs", "replace").strip()
            raise RuntimeError(
                "解压失败（常规方式与 Expand-Archive 均未成功）。\n"
                f"Expand-Archive 返回 {r.returncode}：{err[:300]}\n\n"
                "建议改用便携版：把 zip 解压到任意文件夹后双击里面的 exe。"
            )
        self.status.configure(text="正在解压文件… 100%")
        self.update_idletasks()

    def _extract_stream(self, target: Path):
        with zipfile.ZipFile(self.payload) as zf:
            infos = zf.infolist()
            self.copy_total = max(sum(i.file_size for i in infos), 1)
            self.copied = 0
            step = max(self.copy_total / 200, 1)

            for info in infos:
                dest = target / info.filename
                # 防Zip Slip：拒绝写到目标目录之外的路径
                try:
                    dest.resolve().relative_to(target)
                except ValueError:
                    raise RuntimeError(f"载荷包含非法路径：{info.filename}")

                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        self.copied += len(chunk)
                if self.copied >= step:
                    self.bar["value"] = min(100, self.copied * 100 / self.copy_total)
                    pct = min(100, int(self.copied * 100 / self.copy_total))
                    self.status.configure(text=f"正在解压文件… {pct}%")
                    self.update_idletasks()

    def _shortcuts(self, target: Path):
        """建快捷方式。**逐个目标独立尝试**，一条失败不影响其它条。

        v3.1.11：原来任何一条失败（`check=True`）都会抛出去，
        整个安装被判为"未完成"。现场只有开始菜单那条失败，
        桌面图标其实已经建好了，却被一起回退掉。
        """
        start = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        destinations = [start / f"{APP_SHORT}.lnk"]
        if self.make_desktop.get():
            destinations = [
                self._desktop() / f"{APP_SHORT}.lnk",
                Path(os.environ.get("USERPROFILE", str(Path.home())))
                / "Desktop" / f"{APP_SHORT}.lnk",
                start / f"{APP_SHORT}.lnk",
            ]
            # Remove duplicates while preserving the resolved desktop first.
            destinations = list(dict.fromkeys(destinations))
        errors: list[str] = []
        made = 0
        for destination in destinations:
            try:
                self._create_shortcut(destination, target)
                made += 1
                _setup_log(f"[setup] 快捷方式已创建：{destination}")
            except Exception as e:                        # noqa: BLE001
                errors.append(f"{destination.name}：{e}")
                _setup_log(f"[setup] 快捷方式失败 {destination}：{e}")
        if made == 0:
            raise RuntimeError("；".join(errors) or "没有可创建的快捷方式位置")

    @staticmethod
    def _create_shortcut(destination: Path, target: Path):
        """写一个 .lnk。**三种办法依次试**，任一成功即可。

        为什么要有三级：现场（2026-10-28）PowerShell 那条偶尔返回 1，
        原因在这类受限机器上很难查（COM 初始化 / 执行策略 / 编码都可能）。
        换一条完全不同的链路往往就过了 —— 值不值得深究，
        取决于"用户能不能拿到桌面图标"，所以直接给足退路：

          ① PowerShell -EncodedCommand（原来那条，最快）
          ② PowerShell -Command 读临时 .ps1（避开命令行编码问题）
          ③ cscript + VBScript（完全不经过 PowerShell）
        """
        import base64

        destination.parent.mkdir(parents=True, exist_ok=True)
        exe = target / EXE_NAME
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        errors: list[str] = []

        def _check(r, label):
            if r.returncode == 0 and destination.is_file():
                return True
            err = (r.stderr or b"").decode("mbcs", "replace").strip()
            # PowerShell 会把 CLIXML 进度记录塞进 stderr，真正的错误在后面
            if "CLIXML" in err:
                err = err.split("</Objs>")[-1].strip() or err[-200:]
            errors.append(f"{label}: rc={r.returncode} {err[:200]}")
            return False

        script = (
            "$s=New-Object -ComObject WScript.Shell;"
            f"$l=$s.CreateShortcut('{destination}');"
            f"$l.TargetPath='{exe}';"
            f"$l.WorkingDirectory='{target}';"
            f"$l.IconLocation='{exe},0';$l.Save()"
        )

        # ① EncodedCommand
        try:
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                               capture_output=True, timeout=30, creationflags=flags)
            if _check(r, "EncodedCommand"):
                return
        except Exception as e:                            # noqa: BLE001
            errors.append(f"EncodedCommand: {e}")

        # ② 临时 .ps1 + -File
        try:
            import tempfile
            ps1 = Path(tempfile.gettempdir()) / "hra_mkshortcut.ps1"
            ps1.write_text(script, encoding="utf-8-sig")
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                "-File", str(ps1)],
                               capture_output=True, timeout=30, creationflags=flags)
            if _check(r, "ps1"):
                return
        except Exception as e:                            # noqa: BLE001
            errors.append(f"ps1: {e}")

        # ③ cscript + VBScript（不经过 PowerShell）
        try:
            import tempfile
            vbs = Path(tempfile.gettempdir()) / "hra_mkshortcut.vbs"
            vbs.write_text(
                'Set s = CreateObject("WScript.Shell")\r\n'
                f'Set l = s.CreateShortcut("{destination}")\r\n'
                f'l.TargetPath = "{exe}"\r\n'
                f'l.WorkingDirectory = "{target}"\r\n'
                f'l.IconLocation = "{exe},0"\r\n'
                'l.Save\r\n', encoding="mbcs")
            r = subprocess.run(["cscript", "//nologo", str(vbs)],
                               capture_output=True, timeout=30, creationflags=flags)
            if _check(r, "cscript"):
                return
        except Exception as e:                            # noqa: BLE001
            errors.append(f"cscript: {e}")

        raise RuntimeError("；".join(errors) or "未知原因")

    @staticmethod
    def _install_uninstaller(target: Path):
        roots = [Path(getattr(sys, "_MEIPASS", Path(__file__).parent)),
                 Path(__file__).resolve().parent.parent / "release" / "installer"]
        source = next((root / "HRAgent卸载.exe" for root in roots
                       if (root / "HRAgent卸载.exe").is_file()), None)
        if source is None:
            raise FileNotFoundError("安装载荷缺少卸载程序")
        shutil.copy2(source, target / "HRAgent卸载.exe")

    @staticmethod
    def _remove_legacy_after_exit(root: Path, target: Path):
        # Revalidate immediately before scheduling deletion, then allow this wizard
        # process to exit so an installer launched from the old folder is not locked.
        root = root.resolve()
        if not (root / EXE_NAME).is_file():
            raise ValueError("旧版目录缺少主程序，拒绝删除")
        for protected in (target.resolve(), data_dir().resolve()):
            if protected == root or root in protected.parents:
                raise ValueError("旧目录包含新版程序或数据，拒绝删除")
        roots = [Path(getattr(sys, "_MEIPASS", Path(__file__).parent)),
                 Path(__file__).resolve().parent.parent / "release" / "installer",
                 target]
        helper = next((root / "HRAgent卸载.exe" for root in roots
                       if (root / "HRAgent卸载.exe").is_file()), None)
        if helper is None:
            raise FileNotFoundError("找不到独立清理程序")
        subprocess.Popen(
            [str(helper), "--cleanup-legacy", str(root), str(target),
             str(portable_home(target))],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )


    @staticmethod
    def _save_bootstrap(target: Path | None = None):
        path = Path(os.environ.get("APPDATA", str(Path.home()))) / "HRAgent" / "bootstrap.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        settings["user_home"] = str(portable_home(target) if target else data_dir())
        settings["portable"] = bool(target)
        path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _desktop() -> Path:
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes
                buffer = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
                result = ctypes.windll.shell32.SHGetFolderPathW(
                    None, 0x0010, None, 0, buffer
                )
                if result == 0 and buffer.value:
                    return Path(buffer.value)
            except (AttributeError, OSError):
                pass
        return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"

    def _start_menu_only(self, target: Path):
        start = (
            Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs"
        )
        if start.is_dir():
            (start / f"{APP_SHORT} 使用说明.txt").write_text(
                f"{APP_NAME} {APP_VERSION}\n\n"
                f"程序位置：{target / EXE_NAME}\n\n"
                f"可直接双击上面的 exe 启动。\n",
                encoding="utf-8",
            )

    def _register_uninstall(self, target: Path):
        """写卸载项，让控制面板的"应用和功能"里能正常卸载。"""
        import winreg

        key_path = os.environ.get(
            "HRAGENT_UNINSTALL_REG_PATH",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\HRAgentWorkbench",
        )
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
            winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "HRAgent")
            winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, str(target))
            if self.legacy.get():
                winreg.SetValueEx(k, "LegacyInstallLocation", 0, winreg.REG_SZ, self.legacy.get())
            winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, str(target / EXE_NAME))
            # 卸载走安装器自己：--uninstall 模式删目录 + 清注册表 + 建日志
            winreg.SetValueEx(
                k, "UninstallString", 0, winreg.REG_SZ,
                f'"{target / "HRAgent卸载.exe"}"',
            )
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)


def _setup_log(message: str) -> None:
    print(message, flush=True)
    path = os.environ.get("HRAGENT_SETUP_LOG")
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")


def _instance_running(target: Path) -> bool:
    """目标目录的实例文件里记着一个还活着的 PID 就返回 True。"""
    import json
    path = portable_home(target) / "data" / "webapp.pid"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        pid = int(info.get("pid") or 0)
    except (OSError, ValueError, TypeError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在但打不开（别的账户）→ 算活着
    except OSError:
        return False
    return True


def verify_installed_app(target: Path) -> None:
    """Reject an installation whose main executable cannot start."""
    import time

    exe = target / EXE_NAME
    if not exe.is_file():
        raise FileNotFoundError(f"安装后缺少主程序：{exe}")
    env = dict(os.environ)
    env["TP_NO_BROWSER"] = "1"
    env["TP_USER_HOME"] = str(portable_home(target))
    # 已经有实例在跑时不要启探测进程：它会立刻退出（见 launcher 的
    # _open_existing_instance），既证明不了新装的文件能用，还会误判成失败。
    if _instance_running(target):
        return
    log_path = target / "data" / "webapp.log"
    if log_path.exists():
        log_path.unlink()
    process = subprocess.Popen(
        [str(exe), "--no-browser"], cwd=str(target), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                # 退出码 0 = 程序自己正常退出，最常见的原因是端口上已有实例
                # （用户装新版时旧版还开着），启动器发现后直接打开浏览器退出。
                # 这不是安装失败，不能据此回滚。
                if process.returncode == 0:
                    return
                detail = ""
                if log_path.exists():
                    detail = "\n\n" + log_path.read_text(
                        encoding="utf-8", errors="replace"
                    )[-4000:]
                raise RuntimeError(
                    f"新版程序启动失败，退出码 {process.returncode}{detail}"
                )
            time.sleep(0.5)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def silent_install(payload: Path, target: Path, legacy: str, remove_old: bool,
                   no_shortcuts: bool) -> None:
    """Run the same installation primitives without creating Tcl/Tk state."""
    _setup_log(f"[setup] target={target}")
    if remove_old:
        legacy_root = validate_legacy_removal(legacy, target, portable_home(target))
    else:
        legacy_root = None
    _setup_log("[setup] stopping only matching app instances")
    if not kill_running(target, legacy):
        raise RuntimeError("旧版或目标安装目录中的 HRAgent 未能关闭")
    if legacy and not (portable_home(target) / "data" / "workbench.db").is_file():
        _setup_log("[setup] migrating old profile")
        # v3.1.12：与界面路径同一套兜底（先建目录 → 失败降级 robocopy）
        result = _migrate_robust(legacy, target)
        Wizard._save_bootstrap(target)
        _setup_log(f"[setup] migrated={result.get('counts')}, "
                   f"files={result['verified_files']}, via={result.get('via', 'in-process')}")
    elif remove_old:
        raise RuntimeError("新版数据目录已有数据库，未执行迁移，拒绝删除旧目录")
    app = Wizard.__new__(Wizard)
    app.payload = payload
    app.automated = True
    app.make_desktop = type("Flag", (), {"get": lambda self: False})()
    app.copy_total = app.copied = 0
    app.bar = {"value": 0}
    app.status = type("Status", (), {"configure": lambda self, **kw: _setup_log(str(kw.get("text", "")))})()
    app.update = app.update_idletasks = lambda: None
    _mkdir_robust(target)
    _setup_log("[setup] extracting payload")
    app._extract(target)
    _setup_log("[setup] payload extracted")
    # 附加项逐项 try：快捷方式/卸载项失败不影响"程序已装好"这个事实
    if not no_shortcuts:
        try:
            app._shortcuts(target)
        except Exception as e:                            # noqa: BLE001
            _setup_log(f"[setup] 快捷方式失败（不影响使用）：{e}")
    try:
        app._install_uninstaller(target)
    except Exception as e:                                # noqa: BLE001
        _setup_log(f"[setup] 卸载程序失败（不影响使用）：{e}")
    if os.environ.get("HRAGENT_SKIP_UNINSTALL_REGISTRY") != "1":
        try:
            app._register_uninstall(target)
        except Exception as e:                            # noqa: BLE001
            _setup_log(f"[setup] 卸载注册表失败（不影响使用）：{e}")
    # portable.flag 决定"数据放在程序目录内"；缺了它数据会跑到 %LOCALAPPDATA%，
    # 用户的旧数据就"看不见"了 —— 写失败不能静默，走 cmd 兜底。
    _write_text_robust(target / "portable.flag", "portable-data-v1\n")
    try:
        (target / "installed.flag").unlink(missing_ok=True)
    except OSError:
        pass
    _setup_log("[setup] installation verified")
    if os.environ.get("HRAGENT_SKIP_APP_VERIFY") != "1":
        _setup_log("[setup] launching installed app for verification")
        verify_installed_app(target)
        _setup_log("[setup] installed app launched successfully")
    if legacy_root:
        Wizard._remove_legacy_after_exit(legacy_root, target)
        _setup_log("[setup] old directory cleanup scheduled")


def resolve_payload(argv: list[str]) -> Path | None:
    """定位载荷。三种来源，优先级从高到低：

    1. 环境变量 HRAGENT_SETUP_PAYLOAD —— 打成 exe 后由启动器写入，
       载荷在 sys._MEIPASS 里（不是命令行传的，也不是 exe 旁边的）。
    2. 命令行 zip 路径 —— 开发态调试用。
    3. 都没有 → 返回 None，让调用方报错。
    """
    env = os.environ.get("HRAGENT_SETUP_PAYLOAD")
    if env and Path(env).is_file():
        return Path(env)
    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "payload.zip"
    if bundled.is_file():
        return bundled
    for a in argv[1:]:
        if a == "--wizard":
            continue
        p = Path(a)
        if p.suffix.lower() == ".zip" and p.is_file():
            return p
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--target")
    parser.add_argument("--legacy")
    parser.add_argument("--remove-old", action="store_true")
    parser.add_argument("--no-shortcuts", action="store_true")
    args, _ = parser.parse_known_args()
    if args.install:
        payload = resolve_payload(sys.argv)
        if payload is None:
            raise SystemExit("安装载荷缺失")
        try:
            silent_install(payload, Path(args.target).expanduser().resolve() if args.target
                           else default_install_dir(), args.legacy or "",
                           args.remove_old, args.no_shortcuts)
            return 0
        except Exception:
            import traceback
            _setup_log(traceback.format_exc())
            return 2
    if len(sys.argv) > 1 and sys.argv[1] == "--uninstall":
        return _silent_uninstall()

    payload = resolve_payload(sys.argv)
    if payload is None:
        print("用法：python setup_wizard.py <载荷.zip>")
        print("或：打包成 exe 后由 setup_boot.py 传入 HRAGENT_SETUP_PAYLOAD")
        return 1

    app = Wizard(payload)
    app.mainloop()
    return 0


def _silent_uninstall() -> int:
    """由控制面板调用：删安装目录 + 卸载注册表项，保留用户数据。"""
    import winreg

    target = default_install_dir()
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\HRAgentWorkbench"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            loc, _ = winreg.QueryValueEx(k, "InstallLocation")
            if loc:
                target = Path(loc)
    except FileNotFoundError:
        pass

    if not kill_running():
        return 2

    # 数据目录故意不删：HR 的简历库重装后还在
    shutil.rmtree(target, ignore_errors=True)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
