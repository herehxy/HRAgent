# -*- coding: utf-8 -*-
"""起一个**源码版**服务，用于截图与浏览器端无障碍实测。

为什么不直接双击 exe：exe 是上一次构建的产物，不含刚改的前端。
本脚本指到一份独立的演示数据副本，不碰真实用户库。

用法：
    python tools/_serve_for_ui.py [port]
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "repo_src")
SEED = os.path.join(BASE, "_pack_data")

home = os.environ.get("HRA_UI_HOME") or os.path.join(
    tempfile.gettempdir(), "hra_ui_check")


def prepare() -> None:
    """准备一份可写的演示数据（首次复制种子，之后复用）。"""
    data = os.path.join(home, "data")
    cfg = os.path.join(home, "config")
    os.makedirs(data, exist_ok=True)
    os.makedirs(cfg, exist_ok=True)
    if not os.path.isfile(os.path.join(data, "workbench.db")):
        seed_data = os.path.join(SEED, "data")
        if os.path.isdir(seed_data):
            for name in os.listdir(seed_data):
                s = os.path.join(seed_data, name)
                d = os.path.join(data, name)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
    pkg_cfg = os.path.join(BASE, "_package_cfg")
    if os.path.isdir(pkg_cfg):
        for name in os.listdir(pkg_cfg):
            s = os.path.join(pkg_cfg, name)
            d = os.path.join(cfg, name)
            if os.path.isfile(s) and not os.path.exists(d):
                shutil.copy2(s, d)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    prepare()
    os.environ["TP_USER_HOME"] = home
    os.environ["TP_DATA_DIR"] = os.path.join(home, "data")
    os.environ["TP_CONFIG_DIR"] = os.path.join(home, "config")
    sys.path.insert(0, SRC)
    os.chdir(SRC)

    import uvicorn
    from app.server import app

    print(f"[i] 数据目录：{home}")
    print(f"[i] 地址：http://127.0.0.1:{port}/")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
