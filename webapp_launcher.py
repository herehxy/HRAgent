"""HRAgent Web 应用启动器（PyInstaller 入口，带任务栏托盘）。

启动后发生的事：
1. 以程序资源根（onedir 的 _internal/）为基准；
2. data/workbench.db 不存在时自动建库 + 建 HR 账号（等价于 cli.py init 的最小集）；
3. 子线程启动 FastAPI（uvicorn），主线程进入任务栏托盘循环；
4. 托盘菜单可「打开工作台 / 打开数据目录 / 退出」——退出会优雅停掉服务再结束进程。

数据落点（与源码运行的 __file__ 推导一致）：
- 数据库/原件/收信/备份： <程序目录>/_internal/data/
- 密钥与配置：            <程序目录>/_internal/config/
- 运行日志：              <程序目录>/_internal/data/webapp.log（无控制台窗口时靠它排错）
升级版本前请先备份 _internal/data/，再覆盖程序文件。
"""
from __future__ import annotations

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


def _base_dir() -> str:
    """程序资源根目录。

    PyInstaller onedir 模式下 sys._MEIPASS = <exe目录>/_internal，
    app/*.py 里各模块用 __file__ 推导出的 BASE 也指向这里，保持一致。
    源码直跑时退化为本文件所在目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    return meipass or os.path.dirname(os.path.abspath(__file__))


def _redirect_stdio_to_log(base: str):
    """无控制台窗口时，把 stdout/stderr 写进日志文件，避免异常无处可查。

    日志写在**用户数据目录**而不是包内 `data/`：包内那个在安装目录下可能
    只读，打开日志文件本身就会失败——而"打不开日志"恰好是排障时最需要它的
    时刻。这里 try 一圈，包内可写就用包内（路径短、用户找得到），否则回退。
    """
    if not getattr(sys, "frozen", False):
        return None
    for d in (os.path.join(base, "data"),):
        try:
            os.makedirs(d, exist_ok=True)
            log_path = os.path.join(d, "webapp.log")
            # buffering=1 行缓冲：无控制台时日志必须即刻落盘，否则异常发生时最后几行会丢
            fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
            sys.stdout = fh
            sys.stderr = fh
            print(f"\n===== 启动 {__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S} =====")
            return log_path
        except Exception:
            continue
    return None


def _pick_data_dir(base: str) -> str:
    """决定数据目录：优先可写，且不写进程序安装目录。

    为什么必须挑可写目录（实测踩过）：包内 `data/` 是 PyInstaller 打出来的
    datas，程序装到 `C:\\Program Files\\...` 或由别的账户运行时可能只有读权限，
    而 SQLite 的 WAL 模式还要在同目录建 `workbench.db-wal` / `-shm`——建不出来
    就直接 `attempt to write a readonly database`，简历一条都存不进去，
    而且报错发生在后台分析任务里，前台看不出来。

    策略：
      1. 用户显式设了 TP_DATA_DIR →听用户的；
      2. 包内 data/ 可写 → 就地用它（单机单实例用户的常见情况，路径最短）；
      3. 不可写 → 退到 exe 同级的 `HRAgent数据` 目录（一定可写，因为它跟
         程序放在一起；如果这也写不了，再退到 %LOCALAPPDATA%）。
    每次启动都重新判断，这样"从 Program Files 拷到 D 盘"这类移动也能自愈。
    """
    inner = os.path.join(base, "data")
    probe = os.path.join(inner, ".wtest")
    try:
        os.makedirs(inner, exist_ok=True)
        with open(probe, "w") as f:
            f.write("1")
        os.remove(probe)
        return inner
    except Exception as exc:
        print(f"[i] 包内 data 不可写（{type(exc).__name__}: {exc}），改用用户数据目录")

    exe_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                              else sys.argv[0]))
    for cand in (os.path.join(exe_dir, "HRAgent数据"),
                 os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
                              "HRAgent数据")):
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".wtest")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            print(f"[√] 数据目录：{cand}")
            return cand
        except Exception:
            continue
    raise RuntimeError("找不到可写的数据目录：请设置环境变量 TP_DATA_DIR 指定一个可写路径")


def _ensure_db(base: str) -> str:
    """定位/初始化数据库，返回路径。

    首次启动（可写目录里还没有库）时，把包内的演示库**复制**过去而不是直接
    使用它：包内那份在安装目录下是只读的，SQLite 无法在旁建 WAL 文件。
    """
    from app import auth, db

    packaged_db = os.path.join(base, "data", "workbench.db")
    data_dir = os.environ.get("TP_DATA_DIR") or _pick_data_dir(base)
    db_path = os.environ.get("TP_DB_PATH") or os.path.join(data_dir, "workbench.db")

    # 包内有演示库、目标还没有 → 复制一份过去（含原件一并带走）。
    # 注意只有"目标还没有"才复制：第二次启动时库里已有 HR 自己的数据，
    # 绝不能被包内演示库覆盖回去。
    if os.path.isfile(packaged_db) and not os.path.exists(db_path):
        import shutil
        try:
            shutil.copy2(packaged_db, db_path)
            src_archive = os.path.join(base, "data", "archive")
            dst_archive = os.path.join(data_dir, "archive")
            if os.path.isdir(src_archive) and not os.path.isdir(dst_archive):
                shutil.copytree(src_archive, dst_archive)
            print(f"[√] 已初始化演示数据（10 位示例候选人 + 邮件模板）：{db_path}")
        except Exception as exc:
            print(f"[×] 演示数据复制失败（{exc}），将建空库")

    conn = db.connect(db_path)
    try:
        created = auth.ensure_seed_users(conn)
        if created:
            print(f"[√] 已创建 HR 账号：{', '.join(created)}（初始口令 change-me）")
    finally:
        conn.close()
    return db_path


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
        # 数据目录可能已被挪到可写位置（见 _pick_data_dir），菜单里要指对地方
        self.data_dir = os.environ.get("TP_DATA_DIR") or os.path.join(base, "data")

    # -- 菜单动作
    def _open_ui(self, icon=None, item=None):
        webbrowser.open(self.url)

    def _open_data(self, icon=None, item=None):
        os.makedirs(self.data_dir, exist_ok=True)
        os.startfile(self.data_dir)

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


def main() -> int:
    base = _base_dir()
    os.chdir(base)
    log_path = _redirect_stdio_to_log(base)

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
        _ensure_db(base)
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
