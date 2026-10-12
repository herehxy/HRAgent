"""程序根目录的**唯一来源**（v1.23.1）。

问题：9 个模块各自用 `dirname(__file__)` 推根目录，谁也不认 `TP_HOME`。
后果：我用 `TP_HOME` 把源码版指到已安装目录时，server.py 读的是 D: 的
`data/` 与 `config/`，而 `agent/llm.py`（模型配置）还在读 repo_src 的 —— 
**模型实际是关着的**，而界面看起来一切正常。

所以根目录只能有一个来源：本模块。凡是要定位 `config/` / `data/` 的地方，
都从这里取，不要再自己算。

`TP_HOME` 的用途：让"源码跑但复用已安装目录的数据与配置"成为一等公民——
开发和压测都需要它（压测尤其：绝不碰用户的真实库）。
"""
from __future__ import annotations

import os

#: 程序根（含 config/ 与 data/）。
#: 默认按本文件位置推导（app/paths.py → 上两级）；TP_HOME 可覆盖。
BASE = os.environ.get("TP_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

CONFIG_DIR = os.path.join(BASE, "config")

#: 数据目录。默认在 BASE/data；但**允许整体挪走**（TP_DATA_DIR）。
#:
#: 为什么需要它（v1.24.8 实测踩过）：打包分发后程序可能被放到
#: `C:\Program Files\...` 或由权限受限的账户运行，包内 `data/` 只读。
#: 而 SQLite的 WAL 模式必须在**库文件同目录**创建 `-wal` / `-shm`，
#: 建不出来就直接 `attempt to write a readonly database`——
#: 症状很隐蔽：界面能开、能看简历，但新简历一条都存不进去，
#: 报错还只出现在后台分析任务日志里。启动器发现包内不可写时会把
#: TP_DATA_DIR 指到用户可写目录，就靠这里生效。
DATA_DIR = os.environ.get("TP_DATA_DIR") or os.path.join(BASE, "data")


def cfg(name: str) -> str:
    """config/ 下某个文件的完整路径。"""
    return os.path.join(CONFIG_DIR, name)


def data(name: str) -> str:
    """data/ 下某个文件（或目录）的完整路径。"""
    return os.path.join(DATA_DIR, name)
