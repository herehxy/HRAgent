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

PORT = int(os.environ.get("TP_PORT") or 8756)


def _base_dir() -> str:
    """程序资源根目录。

    PyInstaller onedir 模式下 sys._MEIPASS = <exe目录>/_internal，
    app/*.py 里各模块用 __file__ 推导出的 BASE 也指向这里，保持一致。
    源码直跑时退化为本文件所在目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    return meipass or os.path.dirname(os.path.abspath(__file__))


def _redirect_stdio_to_log(base: str):
    """无控制台窗口时，把 stdout/stderr 写进日志文件，避免异常无处可查。"""
    if not getattr(sys, "frozen", False):
        return None
    os.makedirs(os.path.join(base, "data"), exist_ok=True)
    log_path = os.path.join(base, "data", "webapp.log")
    # buffering=1 行缓冲：无控制台时日志必须即刻落盘，否则异常发生时最后几行会丢
    fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = fh
    sys.stderr = fh
    print(f"\n===== 启动 {__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S} =====")
    return log_path


def _ensure_db(base: str) -> str:
    """空库则初始化（建表 + 默认账号），返回数据库路径。"""
    from app import auth, db

    data_dir = os.path.join(base, "data")
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.environ.get("TP_DB_PATH") or os.path.join(data_dir, "workbench.db")
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

    # -- 菜单动作
    def _open_ui(self, icon=None, item=None):
        webbrowser.open(self.url)

    def _open_data(self, icon=None, item=None):
        path = os.path.join(self.base, "data")
        os.makedirs(path, exist_ok=True)
        os.startfile(path)

    def _open_log(self, icon=None, item=None):
        path = os.path.join(self.base, "data", "webapp.log")
        if os.path.exists(path):
            os.startfile(path)

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
        print(f"    数据目录：{os.path.join(self.base, 'data')}")
        print("    托盘图标已就绪：右键可打开工作台 / 数据目录 / 退出。")
        self.icon.run()
        print("[√] 服务已停止，进程退出。")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    base = _base_dir()
    os.chdir(base)
    log_path = _redirect_stdio_to_log(base)

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

    url = f"http://127.0.0.1:{PORT}"
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)

    # uvicorn 在新版里只在主线程装 signal handler，子线程跑是安全的
    worker = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    worker.start()

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
