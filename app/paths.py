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

import hashlib
import os

# ---------------------------------------------------------------- 长路径（Windows）
#
# 为什么需要这一层（v2.2.4 实测）：
# Windows 的传统路径上限是 260 字符。**超过它的绝对路径，不加 \\\\?\\ 前缀就访问不到**——
# CreateFileW 报 ERROR_FILENAME_EXCED_RANGE(206)，而 `os.path.isfile` 会把这个错
# 吞成 False。于是症状变成"文件明明在磁盘上，程序却说原件不在"，
# 点"在线查看简历"就是 404，看起来像文件丢了，其实是路径太长。
#
# 真实触发场景有两个，都是"带旧数据"才出现：
# 1. 迁移后路径变长：旧库的绝对路径（旧数据根 + 长文件名）被重写成
#    新数据根 + 同样的长文件名，新数据根再长一点就越 260；
# 2. 附件原始文件名超长：微信/邮件转来的附件名动辄 150+ 字符
#    （实测最长一条 167 字符：cgi-bin_mmwebwx-bin_webwxgetmsgimg__&MsgID=...&skey=@crypt_...），
#    加上时间戳与目录，数据根稍深就越线。
#
# 修法有两条，缺一不可：
# · I/O 一律走 \\\\?\\ 前缀（长路径照样读写）；
# · 落盘前把文件名压到预算内（不制造新的超长路径）。
#   只做前者是"能读了但新文件还在变长"，迟早再破。

#: Windows 传统路径上限（含结尾 NUL）。
MAX_PATH = 260

#: 落盘路径预算：留 20 字符余量给临时后缀、相对拼接与第三方库。
PATH_BUDGET = 240


def _abspath_safe(p: str) -> str:
    """`os.path.abspath` 的安全版：超长路径不让它抛异常。"""
    try:
        return os.path.abspath(p)
    except (OSError, ValueError):
        return os.path.normpath(os.path.join(os.getcwd(), p))


def long_path(path: str) -> str:
    """返回**可以直接做文件 I/O** 的路径；超长的补 \\\\?\\ 前缀（仅 Windows）。

    \\\\?\\ 前缀的语义是"不做任何路径归一化"：不能含 `..`、不能是相对路径、
    分隔符必须是反斜杠。所以先 `abspath` 归一化，再拼前缀。

    短路径原样返回——\\\\?\\ 前缀会改变一些库的行为（比如 `os.path.commonpath`），
    只在真正需要时才用。
    """
    if os.name != "nt" or not path:
        return path
    p = _abspath_safe(str(path))
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):            # UNC：\\\\server\\share → \\\\?\\UNC\\server\\share
        return "\\\\?\\UNC\\" + p[2:]
    if len(p) <= PATH_BUDGET:
        return p
    return "\\\\?\\" + p


def lp_exists(path: str) -> bool:
    return os.path.exists(long_path(path))


def lp_isfile(path: str) -> bool:
    return os.path.isfile(long_path(path))


def lp_isdir(path: str) -> bool:
    return os.path.isdir(long_path(path))


def lp_size(path: str) -> int:
    return os.path.getsize(long_path(path))


def lp_makedirs(path: str, exist_ok: bool = True) -> None:
    os.makedirs(long_path(path), exist_ok=exist_ok)


def lp_open(path: str, *args, **kwargs):
    return open(long_path(path), *args, **kwargs)


def lp_remove(path: str) -> None:
    os.remove(long_path(path))


def lp_listdir(path: str) -> list[str]:
    return os.listdir(long_path(path))


def lp_scandir(path: str):
    return os.scandir(long_path(path))


def lp_walk(path: str):
    return os.walk(long_path(path))


def fit_name(dirpath: str, name: str, ext: str = "",
             budget: int = PATH_BUDGET) -> str:
    """把文件名压到"拼上目录后不超过 budget"。

    压不动就换哈希后缀（~xxxxxxxx），保证：
    · 同一个目录下不同原名不会撞成同一个文件；
    · 原始名字仍完整保存在数据库 file_name 字段里，界面显示不受影响。
    """
    base = os.path.basename(str(name or "").replace("\\", "/")).strip() or "file"
    stem, e = os.path.splitext(base)
    if ext:
        e = ext if ext.startswith(".") else "." + ext
    prefix_len = len(_abspath_safe(dirpath)) + 1
    room = budget - prefix_len - len(e) - 10      # 10: 留给 ~xxxxxxxx
    if room < 12:
        room = 12
    if len(stem) > room:
        digest = hashlib.sha1(stem.encode("utf-8", "replace")).hexdigest()[:8]
        stem = stem[:max(1, room - 9)].rstrip() + "~" + digest
    return stem + e



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
        # 存在性判断必须走 long_path：超长路径（>260）在 Windows 上
        # os.path.exists 会静默返回 False，表现为"文件明明在、程序说没有"。
        if lp_exists(raw):
            return raw
        marker = normalized.lower().rfind("/data/")
        if marker >= 0:
            suffix = normalized[marker + 6:]
            candidate = os.path.join(DATA_DIR, suffix.replace("/", os.sep))
            if lp_exists(candidate) or not lp_exists(raw):
                return candidate
        return raw
    return os.path.join(BASE, raw)
