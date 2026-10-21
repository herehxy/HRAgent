"""HRAgent Web 应用启动器（PyInstaller 入口，带任务栏托盘）。

启动后发生的事：
1. 程序资源只从安装目录读取；用户数据与配置固定在独立目录；
2. 首次启动可安全复制旧版档案，之后启动只使用用户目录，不再碰安装目录；
3. 子线程启动 FastAPI（uvicorn），主线程进入任务栏托盘循环；
4. 托盘菜单可「打开工作台 / 打开数据目录 / 退出」——退出会优雅停掉服务再结束进程。

默认数据落点：
- 数据库/原件/头像/收信/备份： %LOCALAPPDATA%/HRAgentData/data/
- 用户配置与密钥：             %LOCALAPPDATA%/HRAgentData/config/
- 路径设置：                   %APPDATA%/HRAgent/bootstrap.json

安装目录内不再保存任何用户数据。热更新只替换 `_internal` 程序资源，
不会触碰 HRAgentData，也不会替换 master.key 或系统配置。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import traceback
import webbrowser
import time
import tempfile

PORT = int(os.environ.get("TP_PORT") or 8756)
# 端口被占时最多顺延几个（8756 → 8765）。给10 个足够：正常机器上真正冲突
# 只有一个来源（另一个工作台实例），而那通常在几百毫秒内就会被退出流程收走。
PORT_TRIES = 10
APP_ID = "HRAgent"


def _base_dir() -> str:
    """程序资源根目录。

    PyInstaller onedir 模式下 sys._MEIPASS = <exe目录>/_internal，
    app/*.py 里各模块用 __file__ 推导出的 BASE 也指向这里，保持一致。
    源码直跑时退化为本文件所在目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    return meipass or os.path.dirname(os.path.abspath(__file__))


def _bootstrap_path() -> str:
    root = os.environ.get("APPDATA") or os.path.join(
        os.environ.get("USERPROFILE") or tempfile.gettempdir(), "AppData", "Roaming"
    )
    return os.path.join(root, APP_ID, "bootstrap.json")


def _load_bootstrap() -> dict:
    try:
        with open(_bootstrap_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_bootstrap(updates: dict) -> dict:
    from app.profile_migrate import atomic_write_json

    data = _load_bootstrap()
    data.update(updates or {})
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    atomic_write_json(_bootstrap_path(), data)
    return data


def _default_user_home() -> str:
    root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(root, "HRAgentData")


def _resolve_user_home(cli_value: str | None,
                       portable_home: str | None = None) -> str:
    return os.path.abspath(
        os.environ.get("TP_USER_HOME")
        or cli_value
        or portable_home
        or _load_bootstrap().get("user_home")
        or _default_user_home()
    )


def _redirect_stdio_to_log(data_dir: str):
    """无控制台窗口时，把 stdout/stderr 写进日志文件，避免异常无处可查。

    日志写在**用户数据目录**而不是包内 `data/`：包内那个在安装目录下可能
    只读，打开日志文件本身就会失败——而"打不开日志"恰好是排障时最需要它的
    时刻。这里 try 一圈，包内可写就用包内（路径短、用户找得到），否则回退。
    """
    if not getattr(sys, "frozen", False):
        return None
    try:
        os.makedirs(data_dir, exist_ok=True)
        log_path = os.path.join(data_dir, "webapp.log")
        fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = fh
        sys.stderr = fh
        print(f"\n===== 启动 {__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S} =====")
        return log_path
    except Exception:
        pass
    return None


def _ensure_db() -> str:
    """定位/初始化用户数据库；生产包绝不复制演示库。"""
    from app import auth, db

    data_dir = os.environ["TP_DATA_DIR"]
    db_path = os.environ.get("TP_DB_PATH") or os.path.join(data_dir, "workbench.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    from app import profile_migrate
    profile_migrate.normalize_existing_data_paths(db_path, data_dir)

    conn = db.connect(db_path)
    try:
        created = auth.ensure_seed_users(conn)
        if created:
            print(f"[√] 已创建 HR 账号：{', '.join(created)}（初始口令 change-me）")
    finally:
        conn.close()
    return db_path


def _pick_data_dir(base: str) -> str:
    """源码兼容入口。生产环境会在 import app 之前设置 TP_DATA_DIR。"""
    path = os.path.join(base, "data")
    os.makedirs(path, exist_ok=True)
    return path


def _message(text: str, *, error: bool = False) -> None:
    if not getattr(sys, "frozen", False):
        print(text)
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, text, "HRAgent 工作台", 0x10 if error else 0x40
        )
    except Exception:
        print(text)


def _choose_folder(title: str) -> str | None:
    """Win32 文件夹选择器；不依赖 tkinter/Tcl，适合 PyInstaller 小包。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BROWSEINFOW(ctypes.Structure):
            _fields_ = [
                ("hwndOwner", wintypes.HWND),
                ("pidlRoot", ctypes.c_void_p),
                ("pszDisplayName", wintypes.LPWSTR),
                ("lpszTitle", wintypes.LPCWSTR),
                ("ulFlags", wintypes.UINT),
                ("lpfn", ctypes.c_void_p),
                ("lParam", wintypes.LPARAM),
                ("iImage", ctypes.c_int),
            ]

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        display = ctypes.create_unicode_buffer(260)
        info = BROWSEINFOW()
        info.hwndOwner = None
        info.pidlRoot = None
        info.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
        info.lpszTitle = title
        info.ulFlags = 0x1 | 0x10 | 0x40  # 文件系统目录 / 可编辑路径 / 新样式
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            return None
        try:
            path = ctypes.create_unicode_buffer(32768)
            if shell32.SHGetPathFromIDListW(pidl, path):
                return os.path.abspath(path.value)
            return None
        finally:
            ole32.CoTaskMemFree(pidl)
    except Exception as exc:
        print(f"[i] 文件夹选择器不可用：{exc}")
        return None


def _choose_update_file() -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize", wintypes.DWORD),
                ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE),
                ("lpstrFilter", wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR),
                ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD),
                ("lpstrFile", wintypes.LPWSTR),
                ("nMaxFile", wintypes.DWORD),
                ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD),
                ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR),
                ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD),
                ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR),
                ("lCustData", wintypes.LPARAM),
                ("lpfnHook", ctypes.c_void_p),
                ("lpTemplateName", wintypes.LPCWSTR),
            ]

        buffer = ctypes.create_unicode_buffer(32768)
        info = OPENFILENAMEW()
        info.lStructSize = ctypes.sizeof(info)
        info.lpstrFilter = "HRAgent 更新包 (*.zip)\0*.zip\0所有文件 (*.*)\0*.*\0\0"
        info.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
        info.nMaxFile = len(buffer)
        info.lpstrTitle = "选择 HRAgent 热更新包"
        info.Flags = 0x00080000 | 0x00001000 | 0x00000008  # 文件须存在/路径存在/状态栏
        info.lpstrDefExt = "zip"
        if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(info)):
            return os.path.abspath(buffer.value)
    except Exception as exc:
        print(f"[i] 更新包选择器不可用：{exc}")
    return None


def _prepare_profile(base: str, args) -> str:
    """在导入 app 之前固定用户根、配置根与数据根。"""
    from app import profile_migrate

    frozen = bool(getattr(sys, "frozen", False))
    explicit_user_home = os.environ.get("TP_USER_HOME")
    managed_profile = frozen or bool(
        explicit_user_home or args.data_dir or args.migrate_from
    )
    program_dir = (
        os.path.dirname(os.path.abspath(sys.executable))
        if frozen else base
    )
    portable_home = None
    if frozen and not (explicit_user_home or args.data_dir or args.migrate_from):
        marker = os.path.join(program_dir, "portable.flag")
        if (os.path.isfile(marker)
                or profile_migrate.has_profile(program_dir)):
            portable_home = program_dir
            print(f"[i] 便携模式：使用程序目录内的 data/config：{program_dir}")
    user_home = (
        _resolve_user_home(args.data_dir, portable_home)
        if managed_profile else base
    )

    if managed_profile:
        os.environ["TP_USER_HOME"] = user_home
        os.environ.setdefault("TP_CONFIG_DIR", os.path.join(user_home, "config"))
        os.environ.setdefault("TP_DATA_DIR", os.path.join(user_home, "data"))
    elif os.environ.get("TP_HOME"):
        os.environ.setdefault("TP_DATA_DIR", os.path.join(
            os.environ["TP_HOME"], "data"
        ))

    os.makedirs(os.path.join(user_home, "config"), exist_ok=True)
    os.makedirs(os.path.join(user_home, "data"), exist_ok=True)

    requested_legacy = (
        os.environ.get("TP_LEGACY_HOME")
        or args.migrate_from
        or _load_bootstrap().get("legacy_source")
    )
    legacy = None
    if requested_legacy:
        legacy = os.path.abspath(requested_legacy)
        if not profile_migrate.layout(legacy):
            raise ValueError(f"不是有效的旧版数据目录：{legacy}")
    elif managed_profile and frozen and not profile_migrate.has_profile(user_home):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates = profile_migrate.candidate_legacy_roots(base, exe_dir)
        if candidates:
            legacy = candidates[0]

    migrated = False
    if (managed_profile and legacy
            and not profile_migrate.has_profile(user_home)):
        profile_migrate.migrate_profile(legacy, user_home)
        migrated = True
    elif managed_profile and legacy and profile_migrate.has_profile(user_home):
        print("[i] 目标用户目录已有档案，跳过旧版迁移，未覆盖任何数据")

    template = os.path.join(base, "config_template")
    if not os.path.isdir(template):
        template = os.path.join(base, "config")
    profile_migrate.seed_config(template, os.path.join(user_home, "config"))

    if args.migrate_from and profile_migrate.has_profile(user_home) and not migrated:
        raise FileExistsError(
            "目标用户目录已有档案，拒绝自动覆盖："
            + profile_migrate.profile_db(user_home)
        )
    return user_home


# ---------------------------------------------------------------- 托盘

def _load_tray_image(base: str):
    """托盘用图：优先 256px PNG（清晰），否则从 ICO 里挑最大的帧。

    注意不能直接 Image.open('app.ico')——Pillow 打开 ICO 默认停在第一帧，
    而 ICO 里帧按尺寸升序排，拿到的会是 16x16 小图，在高分屏托盘上一拉就糊。
    """
    from PIL import Image

    png = os.path.join(base, "icon_tray.png")
    if os.path.exists(png):
        im = Image.open(png).convert("RGB")  # 不透明，避开托盘 alpha 位图坑
        print(f"[i] 托盘图：icon_tray.png {im.size}")
        return im

    ico = os.path.join(base, "app.ico")
    if os.path.exists(ico):
        im = Image.open(ico)
        try:
            best, best_w = im, im.size[0]
            for i in range(getattr(im, "n_frames", 1)):
                im.seek(i)
                if im.size[0] > best_w:
                    best_w, best = im.size[0], im.copy()
            im = best
        except Exception:
            pass
        im = im.convert("RGB")
        print(f"[i] 托盘图：app.ico 最大帧 {im.size}")
        return im

    print("[i] 托盘图：未找到图标文件，使用兜底纯色块")
    from PIL import ImageDraw
    img = Image.new("RGB", (64, 64), (37, 99, 235))
    ImageDraw.Draw(img).rectangle((16, 16, 47, 47), fill=(255, 255, 255))
    return img


class TrayApp:
    """任务栏托盘：主线程跑消息循环，服务在子线程。"""

    def __init__(self, base: str, server, url: str):
        self.base = base
        self.server = server
        self.url = url
        self.icon = None
        self.data_dir = os.environ.get("TP_DATA_DIR") or os.path.join(base, "data")
        self.config_dir = os.environ.get("TP_CONFIG_DIR") or os.path.join(base, "config")

    # -- 菜单动作
    def _open_ui(self, icon=None, item=None):
        webbrowser.open(self.url)

    def _open_data(self, icon=None, item=None):
        os.makedirs(self.data_dir, exist_ok=True)
        os.startfile(self.data_dir)

    def _open_config(self, icon=None, item=None):
        os.makedirs(self.config_dir, exist_ok=True)
        os.startfile(self.config_dir)

    def _change_data_dir(self, icon=None, item=None):
        chosen = _choose_folder("选择 HRAgent 数据根目录（包含 data 和 config）")
        if not chosen:
            return
        _save_bootstrap({"user_home": chosen})
        _message("数据目录已记录：\n" + chosen +
                 "\n\n退出并重新打开工作台后生效。原有数据不会被删除或覆盖。")

    def _install_update(self, icon=None, item=None):
        package = _choose_update_file()
        if not package:
            return
        install_dir = (
            os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False) else self.base
        )
        updater = os.path.join(install_dir, "HRAgent更新器.exe")
        if not os.path.exists(updater):
            _message("找不到 HRAgent更新器.exe。\n"
                     "请把更新器放在主程序同一目录后重试。", error=True)
            return
        try:
            import subprocess
            subprocess.Popen(
                [updater, "--package", package,
                 "--install-dir", install_dir, "--pid", str(os.getpid())],
                cwd=install_dir,
            )
            print("[update] 已启动更新器，工作台将退出并等待替换程序文件")
            self._quit()
        except Exception as exc:
            _message(f"启动更新器失败：{type(exc).__name__}: {exc}", error=True)

    def _open_log(self, icon=None, item=None):
        # 日志可能写在包内（可写时）或数据目录里，两处都看一眼
        for path in (os.path.join(self.data_dir, "webapp.log"),
                     os.path.join(self.base, "data", "webapp.log")):
            if os.path.exists(path):
                os.startfile(path)
                return

    def _quit(self, icon=None, item=None):
        print("[i] 收到退出指令，正在停止服务...")
        if self.server is not None:
            self.server.should_exit = True
        if self.icon is not None:
            self.icon.stop()

    def run(self):
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("打开工作台", self._open_ui, default=True),
            pystray.MenuItem("打开数据目录", self._open_data),
            pystray.MenuItem("打开配置目录", self._open_config),
            pystray.MenuItem("设置数据目录…", self._change_data_dir),
            pystray.MenuItem("安装热更新包…", self._install_update),
            pystray.MenuItem("查看运行日志", self._open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"服务地址 {self.url}", lambda *a: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出（停止服务）", self._quit),
        )
        self.icon = pystray.Icon("HRAgent工作台", _load_tray_image(self.base),
                                 "HRAgent 工作台", menu)
        # 起托盘后自动开一次浏览器，并提示服务已就绪
        threading.Timer(1.2, self._open_ui).start()
        threading.Timer(1.5, lambda: self.icon.notify(
            f"HRAgent 工作台已启动\n{self.url}", "HRAgent")).start()
        print(f"[√] HRAgent 工作台已启动：{self.url}")
        print(f"    数据目录：{self.data_dir}")
        print("    托盘图标已就绪：右键可打开工作台 / 数据目录 / 退出。")
        self.icon.run()
        print("[√] 服务已停止，进程退出。")


# ---------------------------------------------------------------- 主流程

def _port_busy(port: int) -> int | None:
    """端口是否已被占用；被占用则返回占用它的 PID，否则 None。

    为什么必须提前探：uvicorn跑在子线程里，绑定失败只会往日志写一句，
    主线程照样起托盘、照样弹"已启动"通知——实测表现为
    「双击没反应 / 浏览器打不开 / 托盘里点开是别人（或旧版本）的服务」，
    极难自查。这里先用 socket 探一次，把问题挡在起托盘之前。
    """
    import socket

    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)),):
        with socket.socket(family, socket.SOCK_STREAM) as s:
            # Windows 上对处于 TIME_WAIT / LISTENING 的端口都能连上，
            # 所以这里只需要判断"能不能连上"，不必区分状态。
            s.settimeout(0.6)
            if s.connect_ex(addr) != 0:
                return None
    # 连上了→ 有人占着。反查 PID 只为把提示写得更具体，拿不到也不影响判断。
    try:
        if os.name == "nt":
            import subprocess
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    return int(line.split()[-1])
    except Exception:
        pass
    return -1          # -1 = 占着但没查出 PID


def _is_our_instance(port: int) -> str | None:
    """这个端口上跑的是不是**本程序**？是就返回它的前端版本戳。

    为什么需要它（v2.0.3 单实例）：原来端口被占就顺延 8756→8757…，
    本意是"上一个没退干净的自己，换个端口也能用"。但**实测更常见的场景是
    "用户忘了它还开着、又双击了一次"** —— 于是起了第二个实例，
    两个进程共用同一个 SQLite 库：两个托盘图标、两个端口、两个界面各说各话，
    用户看到的就是"端口冲突 / 打不开 / 数据好像不对"。
    桌面程序的正确行为是**单实例**：已经在跑就把浏览器指过去，自己退出。

    识别方式：`/api/meta` 必带 session/tiers/ui_build 三个键 —— 只认这个特征，
    别的程序占了端口不会被误判成"已有实例"（那种情况仍走顺延）。
    """
    import json
    import urllib.request

    # ⚠️ 必须绕开系统代理：本机装了代理时，连 127.0.0.1 会被代理拦成 502
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/meta", timeout=1.2) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if isinstance(data, dict) and {"session", "tiers", "ui_build"} <= set(data):
        return str(data.get("ui_build") or "")
    return None


def _fail_port(port: int, pid: int | None) -> int:
    """连续 10 个端口都被占用：写日志 + 弹窗告知，然后退出。"""
    who = (f"占用它的进程 PID={pid}" if pid and pid > 0
           else "占用它的进程未知")
    msg = (f"端口 {port}-{port + PORT_TRIES - 1} 都被其他程序占用了（{who}）。\n\n"
           f"请先在系统托盘里右键旧图标 →「退出（停止服务）」，再重新打开。\n"
           f"如果要用指定端口：设置环境变量 TP_PORT 后再启动。")
    print(f"[×] 启动中止：{msg}")
    if not getattr(sys, "frozen", False):
        print("[i] 源码模式下不弹窗（看控制台输出即可）")
        return 1
    try:                # 有界面才弹窗，失败也不该挡住退出
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "HRAgent 工作台", 0x10)
    except Exception as exc:
        print(f"    （弹窗失败：{exc}，详见本日志）")
    return 1


def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(prog="HRAgent工作台", add_help=True)
    parser.add_argument("--data-dir", help="指定用户数据根目录（data/config 的父目录）")
    parser.add_argument("--set-data-dir", nargs="?", const="",
                        help="保存用户数据根目录；不带路径时弹出文件夹选择器")
    parser.add_argument("--migrate-from", nargs="?", const="",
                        help="从指定旧版目录复制档案；不带路径时弹出文件夹选择器")
    parser.add_argument("--show-paths", action="store_true", help="显示当前路径设置")
    parser.add_argument("--runtime-check",
                        help="把关键运行依赖的导入结果写到指定 JSON 文件")
    parser.add_argument("--no-browser", action="store_true", help="启动时不打开浏览器")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    base = _base_dir()
    os.chdir(base)
    args = _parse_args(argv)
    if args.no_browser:
        os.environ["TP_NO_BROWSER"] = "1"

    if args.runtime_check:
        diagnostics = {}
        for name in ("pymupdf", "fitz", "markitdown", "pystray",
                     "PIL", "cv2", "numpy", "rapidocr_onnxruntime"):
            try:
                module = __import__(name)
                diagnostics[name] = {
                    "ok": True,
                    "file": getattr(module, "__file__", ""),
                    "version": getattr(module, "__version__", ""),
                }
            except Exception as exc:
                diagnostics[name] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        os.makedirs(os.path.dirname(os.path.abspath(args.runtime_check)),
                    exist_ok=True)
        with open(args.runtime_check, "w", encoding="utf-8") as fh:
            json.dump(diagnostics, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return 0

    if args.set_data_dir is not None:
        chosen = args.set_data_dir or _choose_folder("选择 HRAgent 数据根目录")
        if not chosen:
            _message("未选择目录，路径设置未改变。")
            return 1
        chosen = os.path.abspath(chosen)
        _save_bootstrap({"user_home": chosen})
        _message("已设置为：\n" + chosen +
                 "\n\n将在下次启动生效。已有数据不会删除或覆盖。")
        return 0

    if args.migrate_from is not None and not args.migrate_from:
        chosen = _choose_folder("选择旧版 HRAgent 安装目录（含 data/workbench.db）")
        if not chosen:
            _message("未选择旧版目录，迁移已取消。")
            return 1
        args.migrate_from = chosen

    for _p in range(PORT, PORT + PORT_TRIES):
        if _port_busy(_p) is None:
            break
        _build = _is_our_instance(_p)
        if _build is None:
            continue
        _url = f"http://127.0.0.1:{_p}"
        print(f"[i] 已有实例在运行（端口 {_p}，前端版本戳 {_build[:12]}），"
              "直接打开浏览器，不再启动第二份")
        if not os.environ.get("TP_NO_BROWSER"):
            try:
                webbrowser.open(_url)
            except Exception as exc:
                print(f"    （打开浏览器失败：{exc}，请手动访问 {_url}）")
        return 0

    try:
        user_home = _prepare_profile(base, args)
    except Exception as exc:
        _message(f"用户目录准备失败：\n{type(exc).__name__}: {exc}", error=True)
        traceback.print_exc()
        return 1

    data_dir = os.environ.get("TP_DATA_DIR") or os.path.join(user_home, "data")
    config_dir = os.environ.get("TP_CONFIG_DIR") or os.path.join(user_home, "config")
    _redirect_stdio_to_log(data_dir)
    if args.show_paths:
        text = (f"程序目录：{base}\n用户数据根：{user_home}\n"
                f"数据目录：{data_dir}\n配置目录：{config_dir}\n"
                f"设置文件：{_bootstrap_path()}")
        _message(text)
        print(text)
        if not getattr(sys, "frozen", False) or args.migrate_from:
            return 1 if args.migrate_from else 0

    # ---- v2.0.3：单实例。已经在跑就直接开浏览器，不再启动第二份 ----
    # 顺序很重要：只探**已被占用**的端口，遇到第一个空闲端口就停 ——
    # 正常冷启动时 8756 是空的，一次探测都不做（不增加启动耗时）。
    for _p in range(PORT, PORT + PORT_TRIES):
        if _port_busy(_p) is None:
            break                      # 这个端口空着，后面不可能有我们的实例
        _build = _is_our_instance(_p)
        if _build is None:
            continue                   # 被别的程序占着，交给下面的顺延逻辑
        _url = f"http://127.0.0.1:{_p}"
        print(f"[i] 已有实例在运行（端口 {_p}，前端版本戳 {_build[:12]}），"
              f"直接打开浏览器，不再启动第二份")
        if not os.environ.get("TP_NO_BROWSER"):
            try:
                webbrowser.open(_url)
            except Exception as exc:
                print(f"    （打开浏览器失败：{exc}，请手动访问 {_url}）")
        return 0

    # 数据目录必须在 import app 之前定下来——app.paths 在**导入时**就根据
    # 环境变量算出 DATA_DIR 并被各模块存成模块级常量，晚一步设就来不及了。
    # 这也是为什么这段要放在 main()最开头，而不是 _ensure_db 里。
    if not os.environ.get("TP_DATA_DIR"):
        try:
            # TP_HOME 是"显式指定根目录"（源码跑但复用已安装目录的数据），
            # 这时数据就该跟着 TP_HOME 走，不能再自己探一个出来。
            # 否则源码版会把数据写进 repo_src/data，与 TP_HOME 的约定相悖。
            home = os.environ.get("TP_HOME")
            os.environ["TP_DATA_DIR"] = (
                os.path.join(home, "data") if home else _pick_data_dir(base))
        except Exception as exc:
            print(f"[×] 数据目录不可用：{exc}")
            return 1
    print(f"[i] 数据目录：{os.environ['TP_DATA_DIR']}")

    try:
        _ensure_db()
    except Exception as exc:
        print(f"[×] 数据库初始化失败：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        if not getattr(sys, "frozen", False):
            raise
        return 1

    import uvicorn
    from app.server import app

    # 端口策略：TP_PORT 起连续探 PORT_TRIES 个，取第一个空闲的；全占才报错。
    # 为什么顺延而不是直接报错退出：最常见的占用者就是"上一个没退干净的自己"
    # （重复双击、上次窗口没关）。这时自动换一个端口立刻能用，比让人先去托盘
    # 里翻图标退出友好得多。实际地址会显示在托盘菜单和启动通知里，不会失联。
    # 注：探测只做到"起托盘前确认已在监听"这一层，端口预留存在理论竞态
    #     （探测→绑定之间被抢），所以绑定失败也要能再顺延，见下面的重试循环。
    server = None
    worker = None
    port = PORT
    last_pid = None
    for i in range(PORT_TRIES):
        cand = PORT + i
        last_pid = _port_busy(cand)
        if last_pid is not None:
            continue
        config = uvicorn.Config(app, host="127.0.0.1", port=cand,
                                log_level="warning")
        server = uvicorn.Server(config)
        # uvicorn 新版只在主线程装 signal handler，子线程跑是安全的
        worker = threading.Thread(target=server.run, name="uvicorn", daemon=True)
        worker.start()

        # 必须等它**真的开始监听**再起托盘。预检只是预检：起托盘的时间点上
        # 服务必须已在监听，否则"托盘在、浏览器打不开"，用户无从判断问题在哪。
        deadline = time.time() + 12
        while time.time() < deadline:
            if getattr(server, "started", False):
                break
            if not worker.is_alive():   # 线程已退 = 绑定失败等致命错误
                break
            time.sleep(0.15)

        if getattr(server, "started", False):
            port = cand
            if i:
                print(f"[i] 端口 {PORT}~{PORT + i - 1} 已被占用，自动改用 {port}")
            break

        # 没起来：多半是探测到绑定之间被抢（竞态），让位给下一个端口重试
        server.should_exit = True
        worker.join(timeout=3)
        print(f"[×] 端口 {cand} 未能监听，换下一个端口重试…")
        server = worker = None
    else:
        return _fail_port(PORT, last_pid)

    if server is None or not getattr(server, "started", False):
        return _fail_port(PORT, last_pid)

    url = f"http://127.0.0.1:{port}"
    print(f"[√] 端口 {port} 已监听，开始起托盘")

    try:
        TrayApp(base, server, url).run()
    except Exception as exc:
        print(f"[×] 托盘启动失败（{type(exc).__name__}: {exc}），退化为纯服务模式")
        traceback.print_exc()
        print(f"    请直接访问 {url}，关闭本窗口退出。")
        try:
            worker.join()
        except KeyboardInterrupt:
            server.should_exit = True
            worker.join(timeout=5)
        return 1

    server.should_exit = True
    worker.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
