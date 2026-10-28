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
# v2.2.4：首选段全不可用时再往后扩 100 个（8766~8865）。
# 为什么加这一段：业务方那台电脑上“端口冲突”是**天天出现**的，
# 10 个连续端口全占/被保留的概率并不低（见 _bind_reason 的说明）。
PORT_TRIES_WIDE = 100
#: 等 uvicorn 真正开始监听的超时（秒）。见主循环里的说明：
#: 必须大于 FastAPI 启动钩子的最坏耗时，否则会留下"被放弃但仍在跑"的实例
#: （uvicorn 先跑 lifespan 再绑端口，且 should_exit 在 startup 期间不生效）。
STARTUP_WAIT = float(os.environ.get("TP_STARTUP_WAIT") or 90)
APP_ID = "HRAgent"


def _base_dir() -> str:
    """程序资源根目录。

    PyInstaller onedir 模式下 sys._MEIPASS = <exe目录>/_internal，
    app/*.py 里各模块用 __file__ 推导出的 BASE 也指向这里，保持一致。
    源码直跑时退化为本文件所在目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    return meipass or os.path.dirname(os.path.abspath(__file__))


def _configure_ca_env(base: str) -> None:
    """Point OpenSSL at the CA bundle shipped in the package.

    The PyInstaller runtime on this machine has no usable system cert store;
    setting the environment path lets urllib/ssl work without overriding any
    global SSL context.
    """
    candidates = [
        os.path.join(base, "cacert.pem"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "cacert.pem"),
        os.path.join(base, "certifi", "cacert.pem"),
        os.path.join(base, "_internal", "cacert.pem"),
        os.path.join(base, "_internal", "certifi", "cacert.pem"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            os.environ.setdefault("SSL_CERT_FILE", path)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", path)
            print(f"[i] 使用包内证书：{path}")
            return
    print("[!] 包内证书不存在，保留系统证书设置")


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
    """记住"上次用哪个数据目录/端口"。**写不进去不许影响启动**。

    v3.1.15（打包验证时发现）：`%APPDATA%\\HRAgent\\` 在某些机器上写不了
    —— 漫游配置文件被策略锁住、企业杀软拦未签名 exe 的写操作、或 APPDATA
    指向只读位置。原来这里直接抛 `PermissionError`，而它是在
    `_prepare_profile` 里**迁移成功之后**被调用的，于是：
        数据已经搬好了 → 只是记不住这回事 → 整个启动被判为失败 → 退出。
    用户看到的是"装好了却打不开"，而真正能用的数据其实已经就位。

    这里只是一份"备忘"（下次优先用哪个目录/端口），丢了顶多下次重新探测一次，
    没有任何理由因为它挡住启动。所以吞掉 OSError 并打印一行。
    """
    from app.profile_migrate import atomic_write_json

    data = _load_bootstrap()
    data.update(updates or {})
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        atomic_write_json(_bootstrap_path(), data)
    except OSError as exc:
        print(f"[!] 记不住启动参数（不影响使用）：{type(exc).__name__}: {exc}")
        print(f"    位置：{_bootstrap_path()}（可能被安全策略限制/只读）")
    return data


def _default_user_home() -> str:
    root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(root, "HRAgentData")


def _home_usable(path: str) -> bool:
    """这个"用户数据根"现在能不能真的用？

    为什么必须验（v3.1.11，现场事故）：
    `bootstrap.json` 里的 `user_home` 是**上次运行时**记下的绝对路径。
    便携版曾经跑在 `D:\\Desktop\\v3`，用户后来把目录删了 / 换成另一台电脑
    （盘符不存在）—— 而 `_resolve_user_home` 原样信任它。
    结果：安装版启动时 `os.makedirs` 直接抛 OSError，
    被 `_prepare_profile` 报成一个 MessageBox，**而弹窗在后台、托盘和浏览器都还没起**
    —— 用户看到的就是"双击没反应 / 只有后台"。

    判据（任一不满足就当不可用，退回默认目录）：
      · 盘符存在（Windows 上没有 D 盘时 makedirs 会抛 WinError 3/53）
      · 能建目录、能写文件（只读盘 / 权限受限的目录同样起不来）
    """
    try:
        parent = os.path.dirname(os.path.abspath(path)) or path
        # 盘符不存在时 isdir(parent) 为 False，且再往上一定也不存在
        if not os.path.isdir(parent) and not os.path.isdir(path):
            # 允许"父目录不存在但可以创建"的情况：逐级往上找到第一个存在的
            probe = parent
            for _ in range(8):
                up = os.path.dirname(probe)
                if up == probe:
                    break
                probe = up
                if os.path.isdir(probe):
                    break
            else:
                return False
            if not os.path.isdir(probe):
                return False
        os.makedirs(path, exist_ok=True)
        probe_file = os.path.join(path, ".write_probe")
        with open(probe_file, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe_file)
        return True
    except OSError:
        return False


def _resolve_user_home(cli_value: str | None,
                       portable_home: str | None = None) -> str:
    """决定用户数据根。**记录值不可信**——必须验一遍再用（见 `_home_usable`）。"""
    explicit = (os.environ.get("TP_USER_HOME") or cli_value or portable_home)
    if explicit:
        return os.path.abspath(explicit)

    recorded = _load_bootstrap().get("user_home")
    if recorded:
        cand = os.path.abspath(str(recorded))
        if _home_usable(cand):
            return cand
        fallback = _default_user_home()
        print(f"[!] 记录的数据目录不可用：{cand}")
        print(f"    改用默认目录：{fallback}（原目录里的数据不会被改动）")
        try:
            _save_bootstrap({"user_home": fallback, "stale_user_home": cand})
        except Exception:                                    # noqa: BLE001
            pass
        return fallback

    return _default_user_home()


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
        # 注意：这里在**启动时**就跑，不只是登录时。老库（v3.0.2 及以前）
        # 只有 `hr` 账号，`ensure_seed_users` 会把它提升为管理员 ——
        # 必须赶在第一个 HTTP 请求之前完成，否则登录进来的人没有管理权限。
        created = auth.ensure_seed_users(conn)
        if created:
            print(f"[√] 已创建管理员账号：{', '.join(created)}（初始口令 change-me，"
                  f"首次登录后请立即修改）")
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
        installed_mode = os.path.isfile(os.path.join(program_dir, "installed.flag"))
        if os.path.isfile(marker):
            portable_home = program_dir
            print(f"[i] 便携模式：使用程序目录内的 data/config：{program_dir}")
    user_home = (
        _resolve_user_home(args.data_dir, portable_home)
        if managed_profile else base
    )
    if portable_home:
        user_home = portable_home

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
    if requested_legacy and not profile_migrate.has_profile(user_home):
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
        try:
            profile_migrate.migrate_profile(legacy, user_home)
            migrated = True
        except Exception as exc:      # noqa: BLE001
            # 绝不能静默失败：用户会看到一个空库，以为数据丢了。
            # 旧目录始终只读，这里失败不会损坏任何东西，只是没迁成。
            print(f"[!] 旧版数据自动迁移失败：{type(exc).__name__}: {exc}")
            print(f"    旧目录：{legacy}")
            print(f"    目标：{user_home}")
            print("    可改用安装包迁移，或手工复制 data/ 与 config/。")
            _message(
                "检测到旧版 HRAgent 数据，但自动迁移未完成：\n"
                f"{type(exc).__name__}: {exc}\n\n"
                f"旧目录（未改动）：\n{legacy}\n\n"
                "程序将以空档案启动。请改用安装包完成迁移，"
                "或手工把旧目录的 data 与 config 复制到新目录。",
                error=True,
            )
    elif managed_profile and legacy and profile_migrate.has_profile(user_home):
        print("[i] 目标用户目录已有档案，跳过旧版迁移，未覆盖任何数据")

    template = os.path.join(base, "config_template")
    if not os.path.isdir(template):
        template = os.path.join(base, "config")
    profile_migrate.seed_config(template, os.path.join(user_home, "config"))

    if migrated:
        _save_bootstrap({"user_home": user_home, "legacy_source": ""})
    if args.migrate_from and not migrated:
        if not profile_migrate.has_profile(user_home):
            raise RuntimeError("旧版迁移未执行：目标目录尚未建立可用档案")
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
        # 必须保留 alpha：图标的四个圆角是透明的，转成 RGB 会把它们填成黑块，
        # 在浅色任务栏上就是四个黑角。pystray 的 win32 后端接受 RGBA。
        im = Image.open(png).convert("RGBA")
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
        im = im.convert("RGBA")     # 同上：保住透明圆角
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
        # 用多级降级的 _open_url，而不是裸 webbrowser.open ——
        # 后者在现场那台机器上会静默失败（见 _open_url 的说明）。
        # `once=False`：这是**用户明确点的**，上次开过也要再开一次
        # （比如他把标签页关了，想重新打开）。
        _open_url(self.url, once=False)

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
        # ⚠️ **这里绝对不要再打开浏览器**（v3.1.14 修）。
        # main() 里已经在起托盘之前打开过一次了；这里再调一次、
        # 或者再挂个 Timer 调一次，用户就会看到**两三个浏览器窗口**——
        # 现场实测：一次启动弹了三个。打开浏览器只允许有**一个**入口，
        # 就是 main() 里那一处。
        threading.Timer(1.5, lambda: self.icon.notify(
            f"HRAgent 工作台已启动\n{self.url}", "HRAgent")).start()
        print(f"[√] HRAgent 工作台已启动：{self.url}")
        print(f"    数据目录：{self.data_dir}")
        print("    托盘图标已就绪：右键可打开工作台 / 数据目录 / 退出。")
        self.icon.run()
        print("[√] 服务已停止，进程退出。")


# ---------------------------------------------------------------- 主流程

def _port_busy(port: int, want_pid: bool = False) -> int | None:
    """端口是否已被占用；被占用则返回占用它的 PID，否则 None。

    `want_pid=False`（默认）时**不查 PID** —— 查 PID 要跑 netstat 子进程
    （~0.5 秒），启动时要探几十个端口，每探一次都跑一遍 netstat
    会让启动慢到"双击像没反应"（实测 25 个占用端口 = 40 多秒）。
    PID 只有在最后要告诉用户"被谁占了"时才需要。

    为什么必须提前探：uvicorn跑在子线程里，绑定失败只会往日志写一句，
    主线程照样起托盘、照样弹"已启动"通知——实测表现为
    「双击没反应 / 浏览器打不开 / 托盘里点开是别人（或旧版本）的服务」，
    极难自查。这里先用 socket 探一次，把问题挡在起托盘之前。
    """
    import socket
    import struct

    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)),):
        with socket.socket(family, socket.SOCK_STREAM) as s:
            # ⚠️ Windows 上 connect 的返回码必须**分类看**，不能只看"是不是 0"：
            #   0     → 连上了 → 有人在听；
            #   10035 (WSAEWOULDBLOCK) / 10060 (超时) → 有人听但接不下
            #          （accept 队列满了，常见于上一个实例的连接还挂着）
            #          —— 这种**算占用**！原来当成"空闲"，于是真去 bind
            #          报 10048，用户看到的就是"后台都退了也有端口冲突"；
            #   10061 (WSAECONNREFUSED) → 没人监听（连接被直接拒掉）；
            #   其它 → 一律按"占用"处理（宁可换端口，别撞车）。
            #
            # 探测套接字设 SO_LINGER(0)：close 时直接发 RST，
            # **不会在对方的 accept 队列里留一个半开连接**——
            # 否则探测几次就把对方队列塞满，之后谁都连不上（自己把自己探测成占用）。
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                             struct.pack("ii", 1, 0))
            except OSError:
                pass
            s.settimeout(0.6)
            rc = s.connect_ex(addr)
            if rc == 0:
                pass                      # 连上了 → 有人在听
            elif rc in (10061, 61):       # ECONNREFUSED → 没人监听
                return None
            elif rc in (10051, 10050, 10065, 51, 50, 65):
                return None              # 网络不可达 → 不可能有本地监听
            # 其余（10035/10060/…）→ 有人在听但接不下 → 算占用
    # 连上了→ 有人占着。反查 PID 只为把提示写得更具体，拿不到也不影响判断。
    if not want_pid:
        return -1
    try:
        if os.name == "nt":
            import subprocess
            raw = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, timeout=5).stdout
            out = raw.decode("gbk", "replace")
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    return int(line.split()[-1])
    except Exception:
        pass
    return -1          # -1 = 占着但没查出 PID


def _instance_file(data_dir: str) -> str:
    """记录"本机正在跑的实例"的 pid+端口。"""
    return os.path.join(data_dir, "webapp.pid")


def _pid_alive(pid: int) -> bool:
    """进程还在不在（Windows 上 os.kill(pid, 0) 就是"查存在性"）。"""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在但我们打不开（别的账户）→ 算活着
    except OSError:
        return False
    return True


def _write_instance_file(data_dir: str, port: int) -> None:
    """写下自己的 pid+端口（供下次启动秒判"是不是我还在跑"）。"""
    import json
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp = _instance_file(data_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "port": port,
                       "started": time.time()}, fh)
        os.replace(tmp, _instance_file(data_dir))
    except OSError:
        pass


def _find_running_instance(data_dir: str):
    """读 PID 文件：进程还活着就返回 (pid, port)，否则返回 None。

    为什么要有它：判断"是不是本程序已经开着"如果只能靠挨个端口发 HTTP，
    慢（每个占用端口 0.6 秒起步）。PID 文件是 O(1) 的。
    进程不在了（用户直接关了进程 / 机器重启）就当没有，顺手把文件清掉。
    """
    import json
    path = _instance_file(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    pid = int(info.get("pid") or 0)
    port = int(info.get("port") or 0)
    if not _pid_alive(pid) or not port:
        try:
            os.remove(path)       # 死实例留下的垃圾，顺手清掉
        except OSError:
            pass
        return None
    return pid, port


def _bind_reason(exc: OSError) -> str:
    """把 bind 失败翻译成人话。

    为什么需要分类（v2.2.4，业务方那台电脑的"端口冲突"就是这么来的）：
    · 10048 WSAEADDRINUSE：端口被别的程序占着（多半是上一个没退干净的自己）；
    · 10013 WSAEACCES：**端口被系统保留** —— Hyper-V / WSL2 / Docker 会预留
      一段动态端口（netsh int ipv4 show excludedportrange protocol=tcp 可见），
      落在保留段里的端口谁也 bind 不了，现象就是"后台都退了，端口还是用不了"；
    · 10049 WSAEADDRNOTAVAIL：地址不可用。
    分不清这三者，用户只能看到一句"端口被占用"，越看越糊涂。
    """
    code = getattr(exc, "errno", None) or 0
    if code in (10048, 48, 98):
        return "已被其他程序占用"
    if code in (10013, 13):
        return "被系统保留（Hyper-V/WSL 等虚拟化组件预留了该端口段，本程序无权绑定）"
    if code in (10049, 99):
        return "地址不可用"
    return f"不可用（{type(exc).__name__}: {exc}）"


def _summary(notes: list, limit: int = 6) -> str:
    """把跳过端口的原因压成一行人话（给日志/提示用）。"""
    if not notes:
        return "（无）"
    head = "、".join(notes[:limit])
    if len(notes) > limit:
        head += f" 等 {len(notes)} 个"
    return head


def _bind_test(port: int, reuse: bool):
    """真去 bind 一次；返回 (能不能绑, 原因)。

    `reuse=False` 是**严格**测试：Windows 上不带 SO_REUSEADDR 的 bind
    遇到"有人在听"或"别人已 bind"都会失败 → 能识别占用。
    `reuse=True` 宽松：Windows 允许它盖在 TIME_WAIT 上（甚至能抢别人的
    监听端口，所以只用于"严格测试失败后再确认一下"）。

    绑完立刻 close：没有建立过连接的套接字不会留下 TIME_WAIT，
    不影响后面 uvicorn 正式绑定（uvicorn 自己也会设 SO_REUSEADDR）。
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if reuse:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True, ""
    except OSError as exc:
        return False, _bind_reason(exc)
    finally:
        try:
            s.close()
        except OSError:
            pass


def _port_usable(port: int):
    """这个端口能不能拿来监听；返回 (能不能, 原因)。

    判定顺序（依据是实测行为，见 _bind_test 上方的说明）：
      1. **严格 bind**（不带 SO_REUSEADDR）—— Windows 上这是唯一可靠的
         "端口是不是真的空着"的判据：有人在听 → 10048；
      2. 失败且是 10013 → 端口落在**系统保留段**（Hyper-V/WSL 预留），
         谁也绑不了 → 换端口；
      3. 失败且是 10048 → 端口拿不到，再试**宽松 bind**：
         能绑上 → 只剩 TIME_WAIT 残留（可安全复用，uvicorn 自己也设
         SO_REUSEADDR）；绑不上 → 真的有人在用，跳过。

    ⚠️ 这里的结论**只依据 bind**，不依据 connect 探测。原因见下面注释。
    """
    ok, why = _bind_test(port, reuse=False)
    if ok:
        return True, ""
    if "保留" in why:
        return False, why
    # 10048：严格 bind 失败 = 端口确实拿不到（有人在听，或别人已 bind 未 listen）。
    #
    # ⚠️ 不要再在这里用 _port_busy 去"确认有没有人监听"——
    # connect 探测在装有安全/过滤软件的机器上会把**空闲**端口也报成
    # WSAEWOULDBLOCK(10035)，当成"有人"就把好端端的端口误杀了（见主循环注释）。
    # 严格 bind 失败本身就是结论，直接试宽松 bind：
    # 能绑上 → 只是 TIME_WAIT 残留，可安全复用；绑不上 → 真的不可用。
    ok2, why2 = _bind_test(port, reuse=True)
    if ok2:
        return True, "（无监听者，可安全复用）"
    # 两次 bind 都失败 → 端口确实不可用。
    # 提示优先用**严格 bind** 的原因（10048"已被占用"是准确结论）；
    # 宽松 bind 常返回 13(WSAEACCES)，那是"覆盖别人的监听端口被拒"的副作用，
    # 直接用它会把"被某程序占用"误报成"被系统保留"，误导用户去查 Hyper-V。
    return False, why if "占用" in why else (why2 or why)


def _os_pick_port():
    """让操作系统分配一个空闲端口（bind 0）。

    兜底用：固定端口全被占/被保留时，**宁可换个地址也不能起不来**——
    实际地址会显示在托盘菜单与启动通知里，用户照样能打开。
    """
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])
        finally:
            s.close()
    except OSError:
        return None


def _patch_pystray_msgfilter() -> None:
    """绕过 pystray 里那句会失败的 `ChangeWindowMessageFilterEx`（v3.1.4）。

    现场实测（2026-10-27）：这台机器上托盘完全起不来，日志是
        File "pystray\\_win32.py", line 257, in _create_window
        PermissionError: [WinError 5] 拒绝访问
    而单独用 ctypes 测 `CreateWindowEx` 是**成功**的（hwnd 非 0），
    所以卡住的不是建窗口，而是紧接着那一句：
        win32.ChangeWindowMessageFilterEx(hwnd, WM_TASKBARCREATED, MSGFLT_ALLOW, None)

    这句的用途（见 pystray 源码注释）是"**以提权身份运行时**也要能收到
    WM_TASKBARCREATED 广播"。它对**非提权进程毫无必要** —— 非提权进程本来
    就能收到广播。而某些安全策略会直接拒绝这个调用（ACCESS_DENIED）。

    所以：把它包一层，失败就**静默跳过**。代价只在"以管理员身份运行且
    需要重建托盘图标"这一种场景下才可能体现（explorer 重启后图标不自动回来），
    而这台机器上原本是"托盘完全不出现"，两害相权取其轻。
    """
    try:
        from pystray import _win32 as _p
        _orig = _p.win32.ChangeWindowMessageFilterEx

        def _safe(hwnd, msg, action, pdata):
            try:
                return _orig(hwnd, msg, action, pdata)
            except Exception as exc:                     # noqa: BLE001
                print(f"[i] 跳过 ChangeWindowMessageFilterEx（{type(exc).__name__}: "
                      f"{exc}）；非提权进程不需要它")
                return True

        _p.win32.ChangeWindowMessageFilterEx = _safe
    except Exception as exc:                             # noqa: BLE001
        print(f"[i] 托盘补丁未生效（不影响其它功能）：{exc}")


#: 本次进程是否已经"把地址交给浏览器"。见 `_open_url` 的说明：
#: 交付动作**不可重试**（重试就是再开一个窗口），所以只允许发生一次。
_BROWSER_OPENED = False


def _open_url(url: str, *, once: bool = True) -> bool:
    """打开浏览器，**最多只把地址交付一次**。

    为什么"最多一次"是硬要求（v3.1.15 现场反馈：一次启动弹出三个浏览器窗口）：
    Chrome / Edge 都是**单实例**程序。已经有一个在跑时，新进程会把 URL 交给
    已有实例，然后自己**立刻退出**。而那个退出码并不可靠 ——
    现场见过 `rc=0`、`rc=21`、`rc=4294930433`（0xFFFFC0FF）。

    原来的写法把"非 0 退出"当成失败，于是依次去试下一个浏览器、
    再试 `os.startfile`、再试 `cmd start` —— **但每一次尝试其实都已经
    把页面送出去了**。三个窗口是这么来的。

    现在的策略：**任何一次尝试只要没抛异常，就认为已交付，立刻停止**。
    代价从"每次多开几个窗口"换成"极少数机器上可能没打开"——
    后者用户可以点托盘菜单「打开工作台」重来（那条路径 `once=False`），
    前者是每天都在烦人的。

    `once=False`：托盘菜单手动点"打开工作台"时用 —— 用户明确要求，
    再来一次是对的。
    """
    global _BROWSER_OPENED
    if once and _BROWSER_OPENED:
        print("[i] 本次启动已经打开过浏览器，跳过重复打开")
        return True

    import subprocess

    # ① 交给 Windows 默认浏览器（ShellExecute）。这是最不容易重复的一条：
    #    它不创建我们自己的子进程，也不会因为"浏览器已在运行"而报错。
    if os.name == "nt":
        try:
            os.startfile(url)                    # noqa: S606
            _BROWSER_OPENED = True
            print(f"[i] 已交给默认浏览器打开：{url}")
            return True
        except Exception as exc:                 # noqa: BLE001
            print(f"    · os.startfile 失败：{type(exc).__name__}: {exc}")

    # ② 直接启动**一个**浏览器（不遍历候选列表：遍历就是重复交付）
    for exe in _browser_candidates()[:1]:
        try:
            subprocess.Popen([exe, url],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _BROWSER_OPENED = True
            print(f"[i] 已用 {os.path.basename(exe)} 打开：{url}")
            return True
        except Exception as exc:                 # noqa: BLE001
            print(f"    · 启动 {os.path.basename(exe)} 失败：{type(exc).__name__}: {exc}")

    # ③ webbrowser（跨平台兜底）
    try:
        if webbrowser.open(url, new=2):
            _BROWSER_OPENED = True
            print("[i] 已用 webbrowser 打开")
            return True
    except Exception as exc:                     # noqa: BLE001
        print(f"    · webbrowser 失败：{type(exc).__name__}: {exc}")

    return False


def _browser_candidates() -> list[str]:
    """系统里**确实存在**的浏览器可执行文件，按偏好排序。

    Chrome 优先（现场用的就是它），其次 Edge（Windows 10/11 必装）。
    只返回真实存在的路径 —— 不猜、不写死盘符。
    """
    out: list[str] = []
    seen: set[str] = set()
    for env, pattern in (
        ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
        ("PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
        ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
        ("PROGRAMFILES(X86)", r"Microsoft\Edge\Application\msedge.exe"),
        ("PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe"),
    ):
        root = os.environ.get(env)
        if not root:
            continue
        p = os.path.join(root, pattern)
        key = os.path.normcase(p)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(p):
            out.append(p)
    return out


def _candidate_ports() -> list:
    """候选端口顺序：**上次成功的端口** → 首选段（8756~8765）→ 扩展段（8766~8865）。

    为什么把"上次成功的端口"提到最前（v3.0.1）：
    端口冲突在这类现场很常见，一旦 8756 被别的程序长期占着，每次启动都要先
    探一遍 8756 才能落到 8757。探测本身不贵（一次 bind），但**地址会变**——
    用户收藏的 `127.0.0.1:8757` 下次可能变成 8758，书签全失效。
    记住上次能用的那个，地址就稳定了；它一旦不可用，后面的顺序与原来完全一致，
    所以这是纯增益，不改变任何既有行为。
    """
    seq = list(range(PORT, PORT + PORT_TRIES)) \
        + list(range(PORT + PORT_TRIES, PORT + PORT_TRIES + PORT_TRIES_WIDE))
    try:
        last = int(_load_bootstrap().get("last_port") or 0)
    except (TypeError, ValueError):
        last = 0
    if last and last in seq:
        seq.remove(last)
        seq.insert(0, last)
    return seq


def _remember_port(port: int) -> None:
    """记住这次成功监听的端口（供下次启动优先使用）。失败不影响启动。"""
    try:
        if int(_load_bootstrap().get("last_port") or 0) != int(port):
            _save_bootstrap({"last_port": int(port)})
    except Exception:                                     # noqa: BLE001
        pass


def _find_existing_instance(max_probes: int = 8):
    """找"本程序已经在跑"的实例，返回 (url, 版本戳, 端口) 或 None。

    ⚠️ **必须扫全部候选端口**（v3.1.11 修）。原来只扫 8756~8765 且探到第 2 个
    占用端口就 break —— 两个后果在真实日志里同时出现过：

      · 实例一旦因为端口顺延漂到 8766 以上（现场日志里出现过 8771/8780），
        再双击就**永远找不到它**，于是又起一份新的；
      · 每份新实例都跑一遍启动钩子（`refresh_skills` 写 8600+ 行技能），
        几个进程一起抢 SQLite 写锁 —— 现场一份日志里
        `database is locked` 出现 **76 次**。

    探测成本：`_bind_test` 一次 bind+close 约 0.1ms，110 个端口也就十几毫秒；
    只有"绑不上"的端口才值得发一次 HTTP（0.6s 超时），所以用 `max_probes` 兜住上界。

    ⚠️ v3.1.12 二次修正（这条很容易再踩）：**不能用"bind 能绑上"判断端口空闲**。
    uvicorn 绑监听套接字时带 `SO_REUSEADDR`，而 Windows 的行为是
    "先绑的那个设了 SO_REUSEADDR，后绑的即使不设也能绑成功"
    （这正是 `SO_EXCLUSIVEADDRUSE` 存在的原因）。实测：第一个实例监听 18900 时，
    `_bind_test(18900)` 返回**成功**，于是这里把它当成空闲端口跳过 ——
    第二个实例照样启动，两边抢同一个库。

    所以改成**并发探测 `/api/meta`**：不依赖 bind，只认服务自报的身份。
    110 个端口 × 0.4 秒超时、16 路并发 → 冷启动（一个服务都没有）时
    全是 ECONNREFUSED，通常 1 秒内跑完；有实例时第一个命中就返回。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ports = list(_candidate_ports())[:120]
    if not ports:
        return None

    # 第一阶段：**快速扫哪些端口有人在听**（connect 一下就知道，关掉的端口
    # 会立刻回 RST，110 个端口总共几毫秒）。这一步把 110 个候选砍到 0~2 个。
    suspects = _listening_ports(ports)
    if not suspects:
        return None

    # 第二阶段：只对"有人在听"的那几个做 HTTP 身份确认（每个最多 ~4 秒）。
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe-inst") as pool:
        futures = {pool.submit(_is_our_instance, p): p for p in suspects}
        try:
            for fut in as_completed(futures, timeout=12):
                try:
                    build = fut.result()
                except Exception:                        # noqa: BLE001
                    continue
                if build is None:
                    continue
                return f"http://127.0.0.1:{futures[fut]}", build, futures[fut]
        except Exception:                                # noqa: BLE001
            pass
    return None


def _listening_ports(ports: list[int], timeout: float = 0.12,
                     budget: float = 3.0) -> list[int]:
    """这些端口里，**有人在听**的是哪些。

    只做 TCP connect，不发 HTTP —— 目的就是把"110 个候选"砍成"真的开着的那几个"，
    后面才值得花时间去做身份确认（一次 HTTP 比一次 connect 贵几十倍）。

    返回值的取舍（宁可多探别漏掉）：
      · connect 成功(0)            → 有人在听，收下
      · ECONNREFUSED(10061/61)     → 明确没人听，跳过
      · 网络不可达(10051/10050/…）  → 跳过
      · **其它（超时/被过滤）**      → 收下：某些安全软件会接管 connect，
                                     对它返回"既不连上也不拒绝"，跳过就会漏掉实例

    `budget` 是**总时间预算**：被过滤的端口每个都要等满 `timeout`，
    110 个就是十几秒（实测把启动拖到 60 秒开外）。超预算就停手，
    用已经收集到的候选继续 —— 找实例本来就是"尽力而为"，
    拖垮启动比偶尔漏一个更糟（漏了也只是多起一个实例，而启动慢是每次双击都难受）。
    """
    import socket
    import struct

    deadline = time.monotonic() + budget
    out: list[int] = []
    for p in ports:
        if time.monotonic() > deadline:
            break
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # 探测连接 close 时直接发 RST，不在对方 accept 队列里留半开连接
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                             struct.pack("ii", 1, 0))
            except OSError:
                pass
            s.settimeout(timeout)
            rc = s.connect_ex(("127.0.0.1", p))
        except OSError:
            out.append(p)
            continue
        finally:
            try:
                s.close()
            except OSError:
                pass
        if rc == 0:
            out.append(p)
        elif rc in (10061, 61):                # ECONNREFUSED
            continue
        elif rc in (10051, 10050, 10065, 51, 50, 65):   # 网络不可达
            continue
        else:
            out.append(p)                      # 超时/被过滤 → 也去确认一下
    return out


def _open_existing_instance() -> bool:
    """已有实例在跑就把它指到浏览器去，自己退出。返回 True 表示"已处理"。

    识别方式与 v2.0.3 一致：`/api/meta` 必带 session/tiers/ui_build 三个键。
    """
    hit = _find_existing_instance()
    if not hit:
        return False
    _url, _build, _p = hit
    print(f"[i] 已有实例在运行（端口 {_p}，前端版本戳 {str(_build)[:12]}），"
          "直接打开浏览器，不再启动第二份")
    if not os.environ.get("TP_NO_BROWSER"):
        if not _open_url(_url):
            _message("工作台已经在运行，但没能自动打开浏览器。\n\n"
                     f"请手动访问：\n{_url}")
    return True


def _port_check_report() -> None:
    """端口体检：把"到底谁占着 / 为什么占着"一次说清。

    业务方那台电脑天天报端口冲突，光看现象没法判断是三种情况里的哪一种：
      ① 端口被别的程序占着（多半是上一个没退干净的自己）；
      ② 端口落在 **系统保留段**（Hyper-V / WSL2 / Docker 预留）——
         这种情况"后台都退了也用不了"，因为压根不是被谁占着，是系统不让绑；
      ③ 端口被别人"已 bind 但没 listen"地占着。
    平时探测只问"连不连得上"，这三者会被混为一谈。

    用法：HRAgent工作台.exe --port-check
    """
    import socket

    out_lines: list[str] = []

    def say(text: str = "") -> None:
        out_lines.append(text)
        print(text)

    say("=" * 62)
    say("HRAgent 端口体检")
    say("=" * 62)
    say(f"首选端口段：{PORT} - {PORT + PORT_TRIES - 1}")
    say(f"扩展端口段：{PORT + PORT_TRIES} - "
        f"{PORT + PORT_TRIES + PORT_TRIES_WIDE - 1}")
    say()
    say(f"{'端口':>6}  {'连得上':<8} {'能绑定':<8} 说明")
    say("-" * 62)
    for p in _candidate_ports()[:PORT_TRIES + 6]:
        busy_pid = _port_busy(p)
        ok, why = _port_usable(p)
        if busy_pid is not None:
            note = f"有程序在监听（PID={busy_pid}）"
        elif not ok:
            note = why
        else:
            note = "空闲可用"
        say(f"{p:>6}  {'是' if busy_pid is not None else '否':<8} "
            f"{'是' if ok else '否':<8} {note}")
    say()
    # 系统保留段（Hyper-V/WSL/Docker）
    reserved = ""
    blocks: list = []
    try:
        import subprocess
        # netsh 的输出是 GBK（中文 Windows），用 utf-8 解会抛 UnicodeDecodeError
        # 把整张体检表搞没——所以按字节取回再按 GBK 解、错的字节替换掉。
        raw = subprocess.run(["netsh", "int", "ipv4", "show", "excludedportrange",
                              "protocol=tcp"], capture_output=True, timeout=8).stdout
        out = raw.decode("gbk", "replace")
        blocks = []
        lines = out.splitlines()
        for i, ln in enumerate(lines):
            parts = ln.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                blocks.append((int(parts[0]), int(parts[1])))
        if blocks:
            reserved = "；".join(f"{a}-{b}" for a, b in blocks)
    except Exception:
        pass
    if reserved:
        say(f"系统保留端口段（Hyper-V/WSL 等虚拟化组件）：{reserved}")
        hit = []
        for a, b in blocks:
            for p in range(PORT, PORT + PORT_TRIES + PORT_TRIES_WIDE):
                if a <= p <= b:
                    hit.append(p)
        if hit:
            say(f"  ⚠ 其中落在我们的候选范围里：{hit[:12]}"
                f"{' …' if len(hit) > 12 else ''}")
        say("  这类端口谁也 bind 不了（报 10013），不是被谁占着。")
    else:
        say("系统保留端口段：未查到（netsh 查询失败或没有保留段）")
    picked = _os_pick_port()
    say()
    say(f"系统现在能分配的端口：{picked if picked else '（拿不到）'}")
    say("（程序在固定端口全不可用时会自动改用它，所以不会因为端口起不来）")
    say("=" * 62)

    # 打包是 windowed 程序（没有控制台）→ 光 print 用户看不见。
    # 所以落一份文件，并弹窗告诉他文件在哪。
    report = "\n".join(out_lines)
    path = ""
    try:
        base_dir = os.environ.get("TP_DATA_DIR") or _base_dir()
        os.makedirs(base_dir, exist_ok=True)     # 目录可能还不存在
        path = os.path.join(base_dir, "port_check.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
    except OSError as exc:
        path = f"（写文件失败：{exc}）"
    # 只在**打包版**弹窗：源码模式下没有界面，MessageBox 会一直等用户点确定
    # （自动化跑测试时会把进程挂住）。TP_NO_MSGBOX=1 可显式关掉。
    if getattr(sys, "frozen", False) and not os.environ.get("TP_NO_MSGBOX"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "端口体检结果已保存到：\n" + path + "\n\n"
                + "要点：\n" + "\n".join(out_lines[-6:]),
                "HRAgent 端口体检", 0x40)
        except Exception:
            pass
    return report


def _is_our_instance(port: int) -> str | None:
    """这个端口上跑的是不是**本程序**？是就返回它的前端版本戳。

    为什么需要它（v2.0.3 单实例）：原来端口被占就顺延 8756→8757…，
    本意是"上一个没退干净的自己，换个端口也能用"。但**实测更常见的场景是
    "用户忘了它还开着、又双击了一次"** —— 于是起了第二个实例，
    两个进程共用同一个 SQLite 库：两个托盘图标、两个端口、两个界面各说各话，
    用户看到的就是"端口冲突 / 打不开 / 数据好像不对"。
    桌面程序的正确行为是**单实例**：已经在跑就把浏览器指过去，自己退出。

    ⚠️ v3.1.12（现场事故）：原来只探 `/api/meta`、超时 0.6 秒。
    而 `/api/meta` 742 人库实测 **585~1584 ms**（它要装配岗位/本体/邮箱配置），
    于是"已有实例"**永远认不出来** —— 每次双击都起新实例，端口一路吃到 8782，
    每份都在跑启动钩子抢写锁（现场日志 `database is locked` 76 次）。

    现在：**先探新增的 `/api/ping`**（O(1)、不查库、几毫秒）。
    只有"ping 有响应但不是本程序"时才退回 `/api/meta`（那才是旧版本实例）——
    **连不上就直接判否**，不做无谓的二次等待：110 个端口逐个多等 2 秒，
    启动就会被拖到几十秒（实测改之前这一条就吃掉 64 秒）。
    """
    import json
    import urllib.request

    # ⚠️ 必须绕开系统代理：本机装了代理时，连 127.0.0.1 会被代理拦成 502
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _read(path: str, timeout: float):
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8", "replace"))

    # ① 新端点：几毫秒就能答，专为"本机有没有人在跑"设计
    try:
        data = _read("/api/ping", 1.2)
    except Exception:                                    # noqa: BLE001
        return None          # 连不上/超时 → 这里不是我们的服务，别再等 /api/meta
    if isinstance(data, dict) and data.get("product") == "HRAgent":
        return str(data.get("ui_build") or "")

    # ② 有响应但格式不对 → 可能是**旧版本**实例（还没有 /api/ping），确认一次
    try:
        data = _read("/api/meta", 2.0)
    except Exception:                                    # noqa: BLE001
        return None
    if isinstance(data, dict) and {"session", "tiers", "ui_build"} <= set(data):
        return str(data.get("ui_build") or "")
    return None


def _fail_port(port: int, pid: int | None, detail: str = "") -> int:
    """所有候选端口都不可用：写日志 + 弹窗告知，然后退出。

    v2.2.4：把**具体原因**带出来（被占用 / 被系统保留 / 未能监听）。
    原来只说一句"都被其他程序占用了"，用户查下去发现根本没有程序在跑
    （其实是端口落在系统保留段里），纯属误导。
    """
    who = (f"占用它的进程 PID={pid}" if pid and pid > 0
           else "占用它的进程未知")
    detail_line = (f"\n具体原因：{detail}" if detail else "")
    msg = (f"端口 {port}-{port + PORT_TRIES + PORT_TRIES_WIDE - 1} 都用不了"
           f"（{who}）。{detail_line}\n\n"
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
    parser.add_argument("--port-check", action="store_true",
                        help="只做端口体检：列出候选端口的占用/保留情况后退出")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    base = _base_dir()
    _configure_ca_env(base)
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

    if args.port_check:
        _port_check_report()
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

    if not args.migrate_from and _open_existing_instance():
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
    # 先看 PID 文件（O(1)）：本程序已经开着就直接把浏览器指过去。
    # 比挨个端口发 HTTP 快几十倍——实测占用端口一多，那条路要等 40 秒。
    _inst = _find_running_instance(os.environ.get("TP_DATA_DIR") or
                                   os.path.join(user_home, "data"))
    if _inst:
        _pid, _p = _inst
        _url = f"http://127.0.0.1:{_p}"
        print(f"[i] 已有实例在运行（PID={_pid}，端口 {_p}），"
              "直接打开浏览器，不再启动第二份")
        if not os.environ.get("TP_NO_BROWSER"):
            # 统一走 `_open_url`：它带"只交付一次"的闸门，
            # 而裸 `webbrowser.open` 既可能静默失败，也不受闸门约束。
            if not _open_url(_url):
                print(f"    （打开浏览器失败，请手动访问 {_url}）")
        return 0

    if _open_existing_instance():
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

    # 端口策略（v2.2.4 重做）：首选段 → 扩展段 → **让系统分配**。
    # 为什么顺延而不是直接报错退出：最常见的占用者就是"上一个没退干净的自己"
    # （重复双击、上次窗口没关）。这时自动换一个端口立刻能用。
    # 为什么加扩展段与系统分配：业务方那台电脑上"端口冲突"天天出现，
    # 光靠 10 个连续端口不够 —— 有时是被别的软件占了，有时是**被系统保留**
    # （Hyper-V/WSL 预留段），有时是"已 bind 未 listen"的漏网套接字。
    # 现在三种都能认出来，并且**永远不会因为端口问题起不来**。
    # 注：探测与正式绑定之间仍有理论竞态（探测→绑定之间被抢），
    #     所以绑定失败也要能再顺延，见下面的重试循环。
    server = None
    worker = None
    port = PORT
    last_pid = None
    skip_notes: list[str] = []
    candidates = _candidate_ports()
    for i, cand in enumerate(candidates):
        # ⚠️ 这里**只能问 _port_usable（严格 bind），不能先用 _port_busy 短路**。
        # _port_busy 靠 connect 探测，而 connect 的返回码在真实机器上并不可靠：
        # 实测本机对**空闲**端口 8756/8757/8888 连过去，返回的是
        # 10035(WSAEWOULDBLOCK) 而不是 10061(ECONNREFUSED)——
        # 本机装有安全/过滤类软件，connect 被它接管，既不连上也不拒绝，
        # 直到超时才罢休（PowerShell 侧同样表现为 TIMEOUT，不是"拒绝"）。
        # 于是"有人在听吗"被误判成"有人"，110 个候选端口**全部**被跳过，
        # 程序每次都只能退化到系统随机端口（日志实测：
        # 「8756(被占用)、8757(被占用)… 等 110 个，已由系统分配端口 53393」）。
        # 后果：用户每次双击地址都变、书签失效、旧实例再也认不出来。
        #
        # 严格 bind 不受影响：实测 8756 strict=OK / reuse=OK，确实是空着的。
        # 所以**严格 bind 才是唯一权威判据**，connect 只能用来解释原因。
        _ok, why = _port_usable(cand)
        if not _ok:
            # 只有在 bind 失败时才反查占用者，把提示写具体（纯为可读性）
            last_pid = _port_busy(cand, want_pid=True)
            if last_pid is not None and last_pid > 0:
                why = f"{why}（PID={last_pid}）"
            skip_notes.append(f"{cand}({why})")
            continue
        config = uvicorn.Config(app, host="127.0.0.1", port=cand,
                                log_level="warning")
        server = uvicorn.Server(config)
        # uvicorn 新版只在主线程装 signal handler，子线程跑是安全的
        worker = threading.Thread(target=server.run, name="uvicorn", daemon=True)
        worker.start()

        # 必须等它**真的开始监听**再起托盘。预检只是预检：起托盘的时间点上
        # 服务必须已在监听，否则"托盘在、浏览器打不开"，用户无从判断问题在哪。
        #
        # ⚠️ v3.0.2：这个超时**必须**大于启动钩子的最坏耗时，否则会出人命——
        # uvicorn 是 `await lifespan.startup()` **之后**才绑端口
        # （uvicorn/server.py:116 → 182 → 205 才置 started），
        # 而 `should_exit=True` 只有在 `main_loop()` 里才生效：
        # startup 还没跑完时设它，线程根本不会退出，`join(3)` 超时返回，
        # 主线程却以为"这个端口失败了"，去试下一个 —— 被放弃的线程跑完照样绑端口成功。
        # 实测 743 人库的钩子要 42 秒，而这里只等 12 秒：
        # 110 个候选端口 × 15 秒 ≈ 27 分钟"双击没反应"，
        # 且每个被放弃的实例都在跑全量 refresh_skills 抢写锁 → 用户点开档案 500。
        #
        # 现在钩子已改为后台线程（server.py 的 _startup_purge_task），正常 1-2 秒就 started；
        # 这里的 90 秒是给"钩子里还有别的慢活"留的余量 —— 宁可多等，
        # 也不能再出现"放弃了却还在后台跑"的实例。
        deadline = time.time() + STARTUP_WAIT
        while time.time() < deadline:
            if getattr(server, "started", False):
                break
            if not worker.is_alive():   # 线程已退 = 绑定失败等致命错误
                break
            time.sleep(0.15)

        if getattr(server, "started", False):
            port = cand
            if i:
                print(f"[i] 端口 {PORT}~{cand - 1} 不可用（{_summary(skip_notes)}），"
                      f"自动改用 {port}")
            break

        # 没起来：多半是探测到绑定之间被抢（竞态），让位给下一个端口重试
        server.should_exit = True
        worker.join(timeout=3)
        skip_notes.append(f"{cand}(未能监听)")
        print(f"[×] 端口 {cand} 未能监听，换下一个端口重试…")
        server = worker = None

    if server is None or not getattr(server, "started", False):
        # 兜底：让操作系统分配一个端口。**起不来才是最糟的**——
        # 换个地址照样能用，地址会显示在托盘菜单与启动通知里。
        picked = _os_pick_port()
        if picked is None:
            return _fail_port(PORT, last_pid, _summary(skip_notes))
        print(f"[i] 固定端口全不可用（{_summary(skip_notes)}），"
              f"已由系统分配端口 {picked}")
        config = uvicorn.Config(app, host="127.0.0.1", port=picked,
                                log_level="warning")
        server = uvicorn.Server(config)
        worker = threading.Thread(target=server.run, name="uvicorn", daemon=True)
        worker.start()
        deadline = time.time() + STARTUP_WAIT
        while time.time() < deadline:
            if getattr(server, "started", False):
                break
            if not worker.is_alive():
                break
            time.sleep(0.15)
        if not getattr(server, "started", False):
            return _fail_port(PORT, last_pid, _summary(skip_notes))
        port = picked

    url = f"http://127.0.0.1:{port}"
    _write_instance_file(os.environ.get("TP_DATA_DIR") or
                         os.path.join(user_home, "data"), port)
    # 记住这个端口：下次启动优先用它，地址不会因为端口顺延而漂移。
    # 放在"已监听"之后——只有真的起来了才值得记。
    _remember_port(port)
    # ---- v3.1.4：**正常启动也要打开浏览器**，而且不能只信 webbrowser ----
    # 原来只有"检测到已有实例"那两条路径会打开浏览器，
    # 真正冷启动的这条路径**从来不打开** —— 只起一个托盘图标。
    # 用户双击后看不到页面，第一反应是"没启动"，于是再双击一次 → 起第二个实例。
    #
    # ⚠️ 光调 `webbrowser.open()` 不够：现场实测（2026-10-27）它**静默失败** ——
    # 不抛异常、也不打开。那台机器 http 默认程序是
    # `ChromeHTML.OLKXVNBPXRE35W2LOOBV35NKFE`（带哈希后缀，说明 Chrome 注册异常），
    # `webbrowser` 走 `os.startfile` 时被 shell 拒绝但没报错。
    # 所以这里改成**逐级降级**，每一级失败都试下一级，最后一定给出可点的地址。
    if not os.environ.get("TP_NO_BROWSER"):
        if not _open_url(url):
            print(f"    （自动打开浏览器失败，请手动访问 {url}）")
            # v3.1.8：**自动打开失败时必须让人看得见地址**。
            # 只写日志等于没说 —— 用户看不到日志，只会认为"双击没反应"。
            _message(
                "HRAgent 工作台已启动。\n\n"
                "系统没能自动打开浏览器（可能被安全软件拦截）。\n"
                f"请手动打开浏览器，访问：\n\n{url}\n\n"
                "（复制上面的地址粘贴到浏览器地址栏即可。）"
            )
    print(f"[√] 端口 {port} 已监听，开始起托盘")

    try:
        _patch_pystray_msgfilter()
        TrayApp(base, server, url).run()
    except Exception as exc:
        # 托盘只是入口之一：服务已经在监听，浏览器也已经打开过。
        # 这里**不能返回非零**——安装器把非零退出码当成"装坏了"并回滚提示，
        # 用户会以为整个程序不能用，而实际上打开网址就能干活。
        #
        # ⚠️ v3.1.4：现场实测这台机器上托盘**必然失败**——
        # `pystray` 建窗口时 `CreateWindowEx` 返回 NULL，随后
        # `ChangeWindowMessageFilterEx` 报 `[WinError 5] 拒绝访问`。
        # 会话是交互式的（SessionId=1、explorer 在跑），所以不是"服务账户"
        # 的问题，而是安全策略拦了建窗口。托盘这条路在这类机器上走不通。
        #
        # 因此降级路径必须**足够明确**：不能只往日志写一行就了事，
        # 用户看不到日志。这里弹一个窗，把地址直接摆出来。
        print(f"[×] 托盘启动失败（{type(exc).__name__}: {exc}），退化为纯服务模式")
        traceback.print_exc()
        print(f"    服务仍在运行，请直接访问 {url}；关闭本窗口即退出。")
        _message(
            "HRAgent 工作台已启动，但**系统托盘图标没能创建**。\n\n"
            f"请用浏览器打开这个地址使用：\n{url}\n\n"
            "（这台机器的安全策略不允许创建托盘窗口。\n"
            "  功能不受影响，只是没有右下角那个图标。\n"
            "  关掉这个提示后，程序会继续在后台运行；\n"
            "  要退出请在本窗口按 Ctrl+C，或用任务管理器结束进程。）"
        )
        try:
            worker.join()
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        worker.join(timeout=5)
        return 0

    server.should_exit = True
    worker.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
