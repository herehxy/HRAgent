"""程序根目录、用户配置与用户数据的**唯一来源**。

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

#: 安装包内置的默认配置。这里只读，升级时随程序替换；用户的真实配置在
#: `CONFIG_DIR`。源码模式两者默认是同一个目录，保持旧行为。
PROGRAM_CONFIG_DIR = os.path.join(BASE, "config")

#: 用户数据根。生产启动器会在 import app 之前设置 `TP_USER_HOME`；
#: 没设置时退回程序根，兼容源码运行和旧版部署。
USER_HOME = os.environ.get("TP_USER_HOME") or BASE

#: 用户配置目录。优先显式 `TP_CONFIG_DIR`；设置 `TP_USER_HOME` 时默认
#: `<用户数据根>/config`；两者都没有时仍用程序根下 `config`（源码模式）。
CONFIG_DIR = (
    os.environ.get("TP_CONFIG_DIR")
    or (os.path.join(USER_HOME, "config")
        if os.environ.get("TP_USER_HOME") else PROGRAM_CONFIG_DIR)
)

#: 数据目录。默认在 BASE/data；但**允许整体挪走**（TP_DATA_DIR）。
#:
#: 为什么需要它（v1.24.8 实测踩过）：打包分发后程序可能被放到
#: `C:\Program Files\...` 或由权限受限的账户运行，包内 `data/` 只读。
#: 而 SQLite的 WAL 模式必须在**库文件同目录**创建 `-wal` / `-shm`，
#: 建不出来就直接 `attempt to write a readonly database`——
#: 症状很隐蔽：界面能开、能看简历，但新简历一条都存不进去，
#: 报错还只出现在后台分析任务日志里。启动器发现包内不可写时会把
#: TP_DATA_DIR 指到用户可写目录，就靠这里生效。
DATA_DIR = os.environ.get("TP_DATA_DIR") or os.path.join(USER_HOME, "data")


def cfg(name: str) -> str:
    """config/ 下某个文件的完整路径。"""
    return os.path.join(CONFIG_DIR, name)


def data(name: str) -> str:
    """data/ 下某个文件（或目录）的完整路径。"""
    return os.path.join(DATA_DIR, name)


def resolve_data_path(value: str) -> str:
    """把库内保存的相对路径解析为当前数据目录下的绝对路径。

    历史数据常见形态是 `data/avatars/12.jpg` / `data/archive/a.pdf`。
    数据根外置后这些路径不能继续相对安装目录解析，否则会指到只读的
    `_internal/data`，表现为“文件明明在、页面却 404”。
    """
    raw = str(value or "")
    if not raw:
        return raw
    normalized = raw.replace("\\", "/")
    if normalized.startswith("data/"):
        return os.path.join(DATA_DIR, normalized[5:].replace("/", os.sep))
    if os.path.isabs(raw):
        if os.path.exists(raw):
            return raw
        marker = normalized.lower().rfind("/data/")
        if marker >= 0:
            suffix = normalized[marker + 6:]
            candidate = os.path.join(DATA_DIR, suffix.replace("/", os.sep))
            if os.path.exists(candidate) or not os.path.exists(raw):
                return candidate
        return raw
    return os.path.join(BASE, raw)
