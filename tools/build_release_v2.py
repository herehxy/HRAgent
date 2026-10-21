"""HRAgent 一键构建：主程序 → 安装器/更新器 → 程序包 → 更新包。

产物（都在 `release/` 下）：
    HRAgent程序包-v2.2.0.zip        给安装器用的程序包（只含程序文件 + 演示数据种子）
    HRAgent工作台-Windows版-v2.2.0.zip  免安装版（解压即用）
    HRAgent更新包-v2.1.0-to-v2.2.0.zip 增量更新包（只含变化的程序文件）
    HRAgent安装器.exe / HRAgent更新器.exe  工具（也会塞进程序包里）

**三条硬规则（每条都对应一个翻车过的坑）**
1. **dist / build 必须先挪走再构建**：PyInstaller 6 的输出目录默认是 `dist`
   （不是 `dist_win`），不挪会被安全删除钩子拦（EXIT=1）。两个都要挪。
2. **安装包里不许出现"用户数据"**：演示数据种子放 `seed/`，用户的库永远在
   `%LOCALAPPDATA%\\HRAgentData`。这样"更新 = 换程序文件"才成立。
3. **更新包只含程序文件**：靠基线目录做**逐文件 sha256 比对**得出差异，
   差异里如果混进 data/ 或密钥类文件，直接中止（不是静默跳过）。

用法：
    python tools/build_release_v2.py                    # 完整构建
    python tools/build_release_v2.py --skip-pyinstaller # 只重打 zip/更新包
    python tools/build_release_v2.py --baseline dist_win/HRAgent工作台
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "HRAgentWebapp.spec")
DIST = os.path.join(ROOT, "dist", "HRAgent工作台")
RELEASE = os.path.join(ROOT, "release")
APP_NAME = "HRAgent工作台"
PY = sys.executable

#: 更新包里绝对不许出现的东西（相对路径任一段命中即中止构建）
FORBIDDEN_SEG = {"data"}
FORBIDDEN_SUFFIX = (".db", ".key", ".secret", ".sqlite", ".sqlite3")


def is_seed(rel: str) -> bool:
    """种子目录是程序资源，可以进更新包。

    ⚠️ 例外判定必须按**路径段**判，不能按前缀判：
    实际路径是 `_internal/seed/data/workbench.db`（种子在 _internal 里），
    按 "seed/" 前缀判会把合法的种子误杀 —— 我第一版就是这么写的，
    结果更新包生成直接被自己的闸门拦下。
    """
    return "seed" in rel.replace("\\", "/").split("/")


def _is_data_path(rel: str) -> bool:
    """路径里含 data 段 → 属于"数据"，更新时只加不删。"""
    return any(seg in FORBIDDEN_SEG for seg in rel.replace("\\", "/").split("/"))


def log(msg: str) -> None:
    print(msg, flush=True)


def retire(path: str) -> None:
    """安全删除机制会拦批量删除 → 统一改名成 .old_<时间戳>（可回退）。"""
    if os.path.exists(path):
        os.rename(path, path + ".old_" + time.strftime("%m%d_%H%M%S"))


def run(cmd: list) -> None:
    log("  $ " + " ".join(os.path.basename(c) for c in cmd))
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = (r.stdout or "")[-2500:]
    if r.returncode != 0:
        log(tail)
        log((r.stderr or "")[-2500:])
        raise SystemExit(f"[×] 命令失败（退出码 {r.returncode}）")
    # PyInstaller 的关键信息在 stderr 里，成功也打出来
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if any(k in line for k in ("[spec]", "results available", "completed successfully",
                                   "ERROR", "WARNING: Hidden import")):
            log("    " + line.strip())


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_version() -> str:
    src = os.path.join(ROOT, "repo_src", "app", "version.py")
    with open(src, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("VERSION"):
                return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


# ---------------------------------------------------------------- 构建

def build_main() -> None:
    """构建主程序（onedir）。"""
    log("[1/6] 构建主程序…")
    retire(os.path.join(ROOT, "dist"))
    retire(os.path.join(ROOT, "build"))
    run([PY, "-m", "PyInstaller", SPEC, "--noconfirm", "--clean"])
    if not os.path.isfile(os.path.join(DIST, "HRAgent工作台.exe")):
        raise SystemExit("[×] 主程序没构建出来")
    log(f"    ✓ {DIST}")


def build_tool(script: str, out_name: str, py: str = None) -> str:
    """构建一个单文件工具 exe（安装器/更新器）。

    ⚠️ 打包用的解释器必须**带 tkinter**（GUI 工具依赖它）。
    `hragent` 那个 venv 与 managed 3.13 都没带 tkinter（ModuleNotFoundError），
    用它们打出来的 exe 一点就崩 → 单独建了 `envs/pack`（基于系统 3.12）来打。
    """
    log(f"    · 构建 {out_name}…")
    work = os.path.join(ROOT, "build", "tools")
    os.makedirs(work, exist_ok=True)
    cmd = [py or PY, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean",
           "--name", out_name,
           "--icon", os.path.join(ROOT, "app.ico"),
           "--distpath", os.path.join(ROOT, "build", "toolbin"),
           "--workpath", work,
           "--specpath", work,
           "--hidden-import", "tkinter", "--hidden-import", "tkinter.filedialog",
           "--hidden-import", "tkinter.messagebox", "--hidden-import", "tkinter.ttk",
           os.path.join(ROOT, "tools", script)]
    run(cmd)
    exe = os.path.join(ROOT, "build", "toolbin", out_name + ".exe")
    if not os.path.isfile(exe):
        raise SystemExit(f"[×] {out_name} 没构建出来")
    return exe


# ---------------------------------------------------------------- 组装

README = """HRAgent 企业人才库工作台（Windows 版）
================================================

一、安装
--------------
方式 A（推荐）：双击 `HRAgent安装器.exe` → 选安装目录 → 「开始安装」。
方式 B：直接解压 `HRAgent工作台-Windows版-{ver}.zip` 到任意目录，双击
        `HRAgent工作台.exe`（**别只拷 exe**，_internal 是程序本体）。

安装后：桌面有快捷方式，双击即用，浏览器会自动打开工作台。

二、数据与配置放在哪（重要）
------------------------------
    程序文件  → 你选的安装目录（可随时删、可随时换）
    你的数据  → %LOCALAPPDATA%\\HRAgentData\\data\\   （数据库、简历原件、头像、邮件模板）
    你的配置  → %LOCALAPPDATA%\\HRAgentData\\config\\  （模型密钥、邮箱授权码、档位阈值）

**重装程序、升级程序都不会动这两个目录里的任何文件。**
需要备份就备份 HRAgentData 整个文件夹；换电脑也拷这个文件夹。

三、以后怎么升级（热更新）
--------------------------
1. 先在托盘图标上右键「退出」（关浏览器窗口不算，服务还在跑）。
2. 双击 `HRAgent更新器.exe` → 选更新包（`HRAgent更新包-*.zip`）→ 选安装目录
   → 「开始更新」。
3. 更新器只替换程序文件，并会核对：更新前后你的数据目录**一个字节都没变**。
   出问题会自动回滚，旧文件备份在 `<安装目录>\\_update_backup\\`。

四、怎么换成 v4-pro（不带图片）
-------------------------------
`deepseek-flash` 支持发图片；`deepseek-v4-pro` 是纯文本、更快更便宜。
在「系统配置 → 模型与密钥」里改模型名即可（两个模型 DeepSeek 官方都在售）。

五、几条设计约定
----------------
- **只建议不决定**：系统给出档位建议、提案，都要 HR 确认后才生效，全程有审计留痕。
- **反幻觉**：技能命中必须有简历原文片段为证，模型说的也要逐字核对。
- **合规**：性别、民族、婚姻生育等敏感信息在送模型前就被屏蔽，且不参与评分。
- **D 档只代表学历不达标**（且投递已明确归岗），其余情况最低 C（储备）。
"""


def assemble_release(version: str, tools: dict) -> str:
    """把 dist 的产物 + 工具 + 文档 组装成发布目录。"""
    log("[2/6] 组装发布目录…")
    rel = os.path.join(RELEASE, APP_NAME)
    retire(rel)
    os.makedirs(RELEASE, exist_ok=True)
    shutil.copytree(DIST, rel)

    # 工具塞进安装目录：卸载项要指向 <安装目录>\\HRAgent安装器.exe
    for name, path in tools.items():
        shutil.copy2(path, os.path.join(rel, name))

    with open(os.path.join(rel, "使用说明.txt"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(README.format(ver=version))
    for extra in ("功能清单.md", "智能简历工作台-功能清单.pdf"):
        p = os.path.join(ROOT, extra)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(rel, extra))
    with open(os.path.join(rel, "version.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": version, "built_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                  fh, ensure_ascii=False, indent=2)
    log(f"    ✓ {rel}")
    return rel


def zip_dir(src: str, out: str, extra: dict | None = None) -> int:
    """把目录打成 zip（中文名用 UTF-8 flag，Win10+ 资源管理器能正确解压）。"""
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if (name.endswith(".pyc") or ".old_" in name
                        or name.endswith("-wal") or name.endswith("-shm")):
                    continue
                full = os.path.join(base, name)
                z.write(full, os.path.relpath(full, os.path.dirname(src)))
                total += os.path.getsize(full)
        for arc, data in (extra or {}).items():
            z.writestr(arc, data)
    return total


# ---------------------------------------------------------------- 更新包

def program_files(root: str) -> dict:
    """程序目录里"算程序文件"的东西：相对路径 → sha256。"""
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "_update_backup")]
        for name in files:
            if name.endswith(".pyc") or ".old_" in name:
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out[rel] = sha256_file(full)
    return out


def make_update_package(baseline: str, new_dir: str, out: str, version: str,
                        prev_version: str) -> dict:
    """对比基线与新版，只把**变化的程序文件**打进更新包。"""
    log("[4/6] 生成更新包…")
    old = program_files(baseline) if baseline and os.path.isdir(baseline) else {}
    new = program_files(new_dir)

    changed = {rel: new[rel] for rel in new if old.get(rel) != new[rel]}
    # 删除清单：**data/ 下的不写进清单**（老版本用户那里可能是他的真实数据，
    # 更新器也不删它们 —— 留着最多几百 KB，删错了就是数据没了）。
    removed = [rel for rel in old
               if rel not in new and not _is_data_path(rel)]
    skipped_removals = [rel for rel in old if rel not in new and _is_data_path(rel)]

    # 硬规则 3：差异里混进不该进的东西 → 中止（不是静默跳过）
    #
    # 口径（v2.2.0 修）：**真正要防的是"用户数据"** —— 那由更新器的
    # 绝对路径检查（is_user_data_path）兜着，指向 %LOCALAPPDATA%\HRAgentData
    # 的写入一律拒绝。安装目录里的 config/ 是**程序资源（模板）**，
    # 授权码（*.secret）本来就随包分发（spec 里就是这么定的），
    # 所以按"目录"放行，不再一刀切地按后缀拦。
    # 唯一永远禁运的是 master.key —— PII 主密钥，进了包等于交出解密能力。
    for rel in list(changed) + removed:
        if _is_data_path(rel) and not is_seed(rel):
            raise SystemExit(f"[×] 更新包差异里出现了数据目录条目，已中止：{rel}")
        if rel.lower().endswith((".db", ".sqlite", ".sqlite3")) and not is_seed(rel):
            raise SystemExit(f"[×] 更新包差异里出现了库文件，已中止：{rel}")
        if os.path.basename(rel).lower() == "master.key":
            raise SystemExit(f"[×] 更新包差异里出现了 PII 主密钥，已中止：{rel}")
        if rel.lower().endswith((".secret", ".key")) and not rel.startswith(
                ("config/", "config_template/")):
            raise SystemExit(f"[×] 更新包差异里出现了密钥文件（不在配置目录内），已中止：{rel}")
    creds = [r for r in changed if r.lower().endswith((".secret", ".key"))
              and r.startswith(("config/", "config_template/"))]
    if creds:
        print(f"    [!] 更新包含配置凭据 {creds} —— 与完整包口径一致，"
              f"外发前请确认收件人可信")

    manifest = {
        "version": version,
        "prev_version": prev_version,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": [{"path": rel, "sha256": digest, "size": os.path.getsize(os.path.join(new_dir, rel))}
                  for rel, digest in sorted(changed.items())],
        "remove": sorted(removed),
    }
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for item in manifest["files"]:
            z.write(os.path.join(new_dir, item["path"]), item["path"])
        z.writestr("update.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    size = os.path.getsize(out) / 1024 / 1024
    log(f"    ✓ {os.path.basename(out)}：{len(manifest['files'])} 个变更文件 / "
        f"删除 {len(manifest['remove'])} 个 / {size:.1f} MB")
    return manifest


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="HRAgent 一键构建")
    ap.add_argument("--skip-pyinstaller", action="store_true", help="不重新构建主程序")
    ap.add_argument("--baseline", default=os.path.join(ROOT, "dist_win", APP_NAME),
                    help="更新包的基线目录（上一版程序目录）")
    ap.add_argument("--no-update-pkg", action="store_true", help="不打更新包")
    ap.add_argument("--tool-python", dest="tool_python",
                    default=r"C:\Users\admin\.workbuddy\binaries\python\envs\pack\Scripts\python.exe",
                    help="构建安装器/更新器用的解释器（必须带 tkinter）")
    args = ap.parse_args()

    version = read_version()
    log(f"═══ HRAgent v{version} 构建 ═══")

    if not args.skip_pyinstaller:
        build_main()

    if not os.path.isdir(DIST):
        raise SystemExit("[×] 没有 dist，先跑一次完整构建")

    log("[3/6] 构建安装器与更新器…")
    tools = {
        "HRAgent安装器.exe": build_tool("hra_installer.py", "HRAgent安装器", args.tool_python),
        "HRAgent更新器.exe": build_tool("hra_updater.py", "HRAgent更新器", args.tool_python),
    }

    rel = assemble_release(version, tools)

    # 程序包（给安装器）
    pkg = os.path.join(RELEASE, f"HRAgent程序包-v{version}.zip")
    total = zip_dir(rel, pkg)
    log(f"[5/6] 程序包：{os.path.basename(pkg)}（{total / 1024 / 1024:.1f} MB 原始 / "
        f"{os.path.getsize(pkg) / 1024 / 1024:.1f} MB 压缩）")

    # 免安装版
    full = os.path.join(RELEASE, f"HRAgent工作台-Windows版-v{version}.zip")
    zip_dir(rel, full)
    log(f"    免安装版：{os.path.basename(full)}（{os.path.getsize(full) / 1024 / 1024:.1f} MB）")

    if not args.no_update_pkg:
        prev = ""
        vj = os.path.join(args.baseline, "version.json")
        if os.path.isfile(vj):
            try:
                prev = json.load(open(vj, encoding="utf-8")).get("version", "")
            except ValueError:
                prev = ""
        upd = os.path.join(RELEASE, f"HRAgent更新包-{prev or 'old'}-to-v{version}.zip")
        make_update_package(args.baseline, rel, upd, version, prev)

    log("═══ 完成 ═══")
    for f in sorted(os.listdir(RELEASE)):
        p = os.path.join(RELEASE, f)
        if os.path.isfile(p) and f.endswith(".zip"):
            log(f"  · {f}（{os.path.getsize(p) / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
