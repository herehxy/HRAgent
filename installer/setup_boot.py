# -*- coding: utf-8 -*-
"""
安装器启动器（会被 PyInstaller 打成 installer.exe 的入口）

职责：
  1. 把内嵌载荷从 sys._MEIPASS 里找到
  2. **把自己复制到临时目录再运行** —— 这是关键：不能在正在运行的
     exe 所在目录解压，也不能让 payload zip 被当成"用户文件"留在安装目录
  3. 把载荷路径传给真正的安装向导

不用一次性释放到临时目录再跑（那会多占 300MB 磁盘）；向导直接从
exe 内部按需读取 zip 流，解压完载荷临时文件自动清理。
"""

import sys
from pathlib import Path

PAYLOAD_NAME = "payload.zip"
APP_SHORT = "HRAgent工作台"


def _payload() -> Path:
    """定位内嵌载荷。开发态（非 frozen）从 ../release/installer 找。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        p = base / PAYLOAD_NAME
        if p.is_file():
            return p
        raise SystemExit(f"安装载荷缺失：{p}")

    # 开发态：退回到仓库里的构建目录，方便本地反复调试
    dev = Path(__file__).resolve().parent.parent / "release" / "installer"
    for cand in sorted(dev.glob("*_payload.zip"), key=lambda x: x.stat().st_mtime):
        return cand
    raise SystemExit(f"开发态：找不到载荷，请先跑 python installer/make_setup.py")


def main() -> int:
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import setup_wizard
    return setup_wizard.main()


if __name__ == "__main__":
    raise SystemExit(main())
