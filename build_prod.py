"""Build the production onedir app, updater, installer, and safe update ZIP."""
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
from pathlib import Path

HERE = os.path.abspath(os.path.dirname(__file__))
WORKSPACE = os.path.dirname(HERE)
BUILD_RUNTIME = os.path.join(HERE, ".runtime_pyinstaller")
RUNTIME_DEPS = os.path.join(HERE, ".runtime_local")
ASSETS = os.path.join(HERE, "build_prod_assets")
TEMPLATE = os.path.join(ASSETS, "config_template")
DIST = os.path.join(HERE, "dist_prod")
WORK = os.path.join(HERE, "build_prod_work")
TOOLS_DIST = os.path.join(HERE, "dist_prod_tools")
RELEASES = os.path.join(HERE, "release_prod")
APP_DIR = os.path.join(DIST, "HRAgent工作台")
APP_EXE = os.path.join(APP_DIR, "HRAgent工作台.exe")
ICON = os.path.join(WORKSPACE, "app.ico")
TRAY_ICON = os.path.join(WORKSPACE, "icon_tray.png")


def _version() -> str:
    sys.path.insert(0, HERE)
    from app.version import VERSION
    return VERSION


def _safe_remove(path: str) -> None:
    """删除构建产物目录；**被占用时给出可操作的提示**，而不是抛原始 traceback。

    现场踩过多次：上一次构建出来的 `release_prod\\HRAgent工作台` 被双击试跑过，
    进程还在后台（有时连托盘都没有，只剩一个跑着 uvicorn 的 exe），
    于是 `data\\webapp.log` 被锁，`shutil.rmtree` 抛
    `PermissionError: [WinError 32] ... 另一个程序正在使用此文件`。
    原始报错只说"文件被占用"，不说"哪个进程"，用户完全无从下手。
    """
    target = os.path.abspath(path)
    if not (target == HERE or target.startswith(HERE + os.sep)):
        raise RuntimeError(f"拒绝清理工作区外目录：{target}")
    if not os.path.exists(target):
        return
    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
    except PermissionError as exc:
        raise RuntimeError(
            f"清理 {target} 失败：文件被占用。\n"
            f"  原因：{exc}\n"
            f"  处理：这个目录是**上一次构建/试跑**留下的，里面可能还有"
            f"正在运行的 HRAgent工作台.exe。\n"
            f"  1) 右下角托盘图标 → 退出；或任务管理器结束所有 HRAgent工作台.exe；\n"
            f"  2) 再重新构建。\n"
            f"  （该目录下的 data/ 只是构建时的空库，不含你的真实数据。）"
        ) from exc


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strip_template_secrets(value):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"api_key", "password", "authorization", "secret",
                           "client_secret", "access_token", "refresh_token"}:
                continue
            clean[key] = _strip_template_secrets(item)
        return clean
    if isinstance(value, list):
        return [_strip_template_secrets(item) for item in value]
    return value


def prepare_config_template() -> None:
    _safe_remove(ASSETS)
    os.makedirs(TEMPLATE, exist_ok=True)
    source = os.path.join(HERE, "config")
    forbidden = {
        "master.key", "secrets.json", "mailbox.json",
        "imap.secret", "smtp.secret",
        "secrets.example.json", "mailbox.example.json",
    }
    copied = 0
    for current, dirs, files in os.walk(source):
        rel = os.path.relpath(current, source)
        target_dir = TEMPLATE if rel == "." else os.path.join(TEMPLATE, rel)
        os.makedirs(target_dir, exist_ok=True)
        dirs[:] = [d for d in dirs if d not in {"__pycache__"}]
        for name in files:
            if (name in forbidden or ".bak" in name.lower()
                    or name.endswith((".pyc", ".pyo", ".tmp"))):
                continue
            source_path = os.path.join(current, name)
            target_path = os.path.join(target_dir, name)
            if name.lower().endswith(".json"):
                try:
                    with open(source_path, encoding="utf-8") as fh:
                        value = json.load(fh)
                    value = _strip_template_secrets(value)
                    with open(target_path, "w", encoding="utf-8") as fh:
                        json.dump(value, fh, ensure_ascii=False, indent=2)
                        fh.write("\n")
                except (OSError, ValueError):
                    continue
            else:
                shutil.copy2(source_path, target_path)
            copied += 1
    _blank_owner_config()
    if not os.path.isfile(os.path.join(TEMPLATE, "stages.json")):
        raise RuntimeError("默认配置模板缺少 stages.json")
    if not os.path.isfile(os.path.join(TEMPLATE, "tiers.json")):
        raise RuntimeError("默认配置模板缺少 tiers.json")
    for name in forbidden:
        if os.path.exists(os.path.join(TEMPLATE, name)):
            raise RuntimeError(f"默认配置模板混入敏感文件：{name}")
    print(f"[prod] 配置模板：{copied} 个文件（已移除真实密钥/邮箱账号/内网端点/岗位尺子）")


#: 发行包里这几份配置**不能**沿用本机的值 —— 它们是"这个人/这家单位"的设置，
#: 不是产品默认。v4.0.0 起一律写成空壳，由使用者自己填。
#:
#: · model.json / embedding.json：本机指向单位内网 MaaS 端点（`ws-*.maas...`），
#:   打进安装包等于把内网地址连同模型名一起发出去；
#: · jd.json：这是**新岗位会继承的默认判定尺子**（钛合金/真空熔铸/材料成型…），
#:   发给别家单位，等于替人家决定"什么算合格"；
#: · interview.json：面试单位/会议室/联系人（含真实电话）是本单位组织架构。
BLANK_CONFIGS = {
    "model.json": {
        "enabled": False,
        "provider": "openai_compatible",
        "base_url": "",
        "model": "",
        "max_tool_rounds": 4,
        "temperature": 0,
        "timeout": 180,
    },
    "embedding.json": {
        "enabled": False,
        "provider": "openai_compatible",
        "base_url": "",
        "model": "",
        "timeout": 60,
        "batch": 16,
    },
    "jd.json": {
        "role": "",
        "department": "",
        "must": {},
        "preferred": {},
    },
    "interview.json": {
        "modes": [],
        "slots": [],
        "rooms": [],
        "units": [],
        "contacts": [],
        "meeting_no": "",
        "meeting_platform": "",
    },
}


def _blank_owner_config() -> None:
    for name, value in BLANK_CONFIGS.items():
        target = os.path.join(TEMPLATE, name)
        if not os.path.isfile(target):
            continue
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write("\n")


def run_pyinstaller(spec: str, dist_path: str, work_path: str,
                    *, clean: bool = True) -> None:
    version_tag = f"{sys.version_info.major}{sys.version_info.minor}"
    if version_tag == "312":
        sys.path.insert(0, RUNTIME_DEPS)
    # Prefer PyInstaller from the active interpreter (the same runtime as the
    # application); fall back to the vendored build copy only when absent.
    try:
        import PyInstaller
    except ImportError:
        sys.path.insert(0, BUILD_RUNTIME)
    from PyInstaller.__main__ import run as run_pyinstaller_main
    command = ["--noconfirm", "--distpath", dist_path, "--workpath", work_path, spec]
    if clean:
        command.insert(3, "--clean")
    print("[prod] PyInstaller " + " ".join(command))
    run_pyinstaller_main(command)


def audit_program_files() -> dict:
    required = [
        APP_EXE,
        os.path.join(APP_DIR, "app.ico"),
        os.path.join(APP_DIR, "icon_tray.png"),
        os.path.join(APP_DIR, "version.json"),
        os.path.join(APP_DIR, "_internal", "config_template", "stages.json"),
    ]
    for path in required:
        if not os.path.isfile(path):
            raise RuntimeError(f"生产包缺少文件：{path}")
    # 托盘库漏打时 PyInstaller 只在 warn 文件里记一行，产物照出——
    # 装到用户机器上就是"启动失败，退出码 1"。这里直接拦死。
    # pystray 是纯 Python 包，通常进 PYZ 而不是落地成 _internal/pystray，
    # 所以要查 exe 里有没有这些模块名，不能只查目录。
    with open(APP_EXE, "rb") as fh:
        blob = fh.read()
    missing = [name for name in (b"pystray._win32", b"pystray._base")
               if name not in blob]
    if missing:
        raise RuntimeError(
            "生产包缺少 pystray（托盘库）：" + ", ".join(m.decode() for m in missing)
            + "。确认 repo_src/.runtime_local/pystray 存在；"
            "它缺失时程序能起服务但托盘不可用。"
        )
    forbidden_names = {
        "workbench.db", "workbench.db-wal", "workbench.db-shm",
        "master.key", "secrets.json", "mailbox.json",
        "imap.secret", "smtp.secret",
    }
    forbidden_dirs = {
        os.path.join(APP_DIR, "_internal", "data"),
        os.path.join(APP_DIR, "_internal", "config"),
    }
    bad: list[str] = []
    for path in forbidden_dirs:
        if os.path.exists(path):
            bad.append(path)
    for current, _dirs, files in os.walk(APP_DIR):
        for name in files:
            if name.lower() in forbidden_names:
                bad.append(os.path.join(current, name))
    if bad:
        raise RuntimeError("生产包混入用户数据/凭据：\n" + "\n".join(bad))
    total = sum(
        os.path.getsize(os.path.join(current, name))
        for current, _dirs, files in os.walk(APP_DIR)
        for name in files
    )
    return {"files": sum(len(files) for _, _, files in os.walk(APP_DIR)),
            "bytes": total}


def make_portable(version: str) -> str:
    output = os.path.join(RELEASES, f"HRAgent工作台-v{version}-便携版.zip")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for current, _dirs, files in os.walk(APP_DIR):
            for name in files:
                source = os.path.join(current, name)
                arcname = os.path.relpath(source, DIST)
                zf.write(source, arcname)
        # v3.1.8：随包带一个「迁移数据.cmd」。
        # 为什么它必须随包：现场那台机器上安装器**无法创建目录**（安全软件
        # 拦截未签名 exe 的写操作），而 robocopy 是微软自带组件、不受限制。
        # 有了这个脚本，用户不需要安装器也能把旧版数据搬过来 —— 实测
        # 741 人的库 + 1107 份附件文件可以完整迁移并正常启动。
        helper = os.path.join(HERE, "build_prod_assets", "迁移数据.cmd")
        if os.path.isfile(helper):
            zf.write(helper, os.path.join("HRAgent工作台", "迁移数据.cmd"))
        tools = os.path.join(WORKSPACE, "release", "portable_tools")
        if os.path.isdir(tools):
            for current, _dirs, files in os.walk(tools):
                for name in files:
                    source = os.path.join(current, name)
                    arcname = os.path.relpath(source, tools)
                    zf.write(source, os.path.join("HRAgent工作台", "portable_tools", arcname))
    return output


def make_update(version: str) -> tuple[str, dict]:
    files: dict[str, str] = {}
    for current, _dirs, names in os.walk(os.path.join(APP_DIR, "_internal")):
        for name in names:
            source = os.path.join(current, name)
            arcname = os.path.relpath(source, APP_DIR).replace(os.sep, "/")
            if arcname.startswith("_internal/data/") or arcname.startswith("_internal/config/"):
                raise RuntimeError(f"更新包出现用户路径：{arcname}")
            files[arcname] = _sha256(source)
    manifest = {
        "product": "HRAgent",
        "version": version,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": "program-only",
        "files": files,
    }
    output = os.path.join(RELEASES, f"HRAgent-update-v{version}.zip")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        zf.writestr("update-manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
        for arcname in files:
            zf.write(os.path.join(APP_DIR, arcname.replace("/", os.sep)), arcname)
    return output, manifest


def release_info(version: str, portable: str, update: str,
                 setup: str | None, info: dict) -> None:
    payload = {
        "product": "HRAgent工作台",
        "version": version,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "program_dir": APP_DIR,
        "portable_zip": portable,
        "portable_sha256": _sha256(portable),
        "update_zip": update,
        "update_sha256": _sha256(update),
        "installer": setup,
        "installer_sha256": _sha256(setup) if setup else None,
        "program_files": info["files"],
        "program_bytes": info["bytes"],
        "user_data_default": "%LOCALAPPDATA%\\HRAgentData",
        "includes": ["app", "config template", "icons", "updater"],
        "excludes": ["workbench.db", "archive", "avatars", "resumes",
                     "mail_in", "outbox", "removed", "backup",
                     "master.key", "mailbox.json", "secrets.json",
                     "imap.secret", "smtp.secret"],
    }
    path = os.path.join(RELEASES, f"HRAgent-v{version}-release.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 HRAgent 生产发行包")
    parser.add_argument("--skip-installer", action="store_true")
    parser.add_argument("--incremental", action="store_true",
                        help="复用 PyInstaller 分析缓存，仅用于快速重打修复")
    args = parser.parse_args(argv)
    if not os.path.isdir(BUILD_RUNTIME):
        raise SystemExit(
            "缺少构建依赖目录 .runtime_pyinstaller；先运行 "
            "python -m pip install --target .runtime_pyinstaller pyinstaller"
        )
    version = _version()
    if not args.incremental:
        for path in (DIST, WORK, TOOLS_DIST, RELEASES):
            _safe_remove(path)
    for path in (DIST, WORK, TOOLS_DIST, RELEASES):
        os.makedirs(path, exist_ok=True)
    prepare_config_template()

    run_pyinstaller("HRAgentProd.spec", DIST, os.path.join(WORK, "app"),
                    clean=not args.incremental)
    updater = os.path.join(TOOLS_DIST, "HRAgent更新器.exe")
    if not (args.incremental and os.path.isfile(updater)):
        run_pyinstaller("HRAgentUpdater.spec", TOOLS_DIST,
                        os.path.join(WORK, "updater"),
                        clean=not args.incremental)
    if not os.path.isfile(updater):
        raise RuntimeError("更新器构建失败")
    shutil.copy2(updater, os.path.join(APP_DIR, "HRAgent更新器.exe"))
    shutil.copy2(ICON, os.path.join(APP_DIR, "app.ico"))
    shutil.copy2(TRAY_ICON, os.path.join(APP_DIR, "icon_tray.png"))
    # v3.1.14：随包带「安装.cmd / 卸载.cmd」这一套自助脚本。
    # 现场机器上**未签名的安装器 exe 无法写磁盘**（安全策略），而 robocopy /
    # PowerShell 是微软签名工具、不受限制。所以把"复制文件 + 建快捷方式 +
    # 写卸载项"做成脚本放进包里：安装器万一起不来，用户双击「安装.cmd」
    # 一样能装好，而且卸载也有入口。
    #
    # ⚠️ 放在 `packaging/` 而不是 `build_prod_assets/` ——
    # 后者每次构建都会被 `prepare_config_template()` 整个删掉重建，
    # 脚本放进去会静默消失（这个坑已经踩过一次）。
    packaging = os.path.join(HERE, "packaging")
    for name in ("install.ps1", "uninstall.ps1", "安装.cmd", "卸载.cmd"):
        src_helper = os.path.join(packaging, name)
        if os.path.isfile(src_helper):
            shutil.copy2(src_helper, os.path.join(APP_DIR, name))
        else:
            print(f"[!] 缺少打包脚本：{src_helper}")
    # Production distribution is portable: launch from this folder and keep
    # data/config beside the executable. Installing flips this marker.
    with open(os.path.join(APP_DIR, "portable.flag"), "w", encoding="ascii") as fh:
        fh.write("portable-data-v1\n")
    installed_flag = os.path.join(APP_DIR, "installed.flag")
    if os.path.isfile(installed_flag):
        os.remove(installed_flag)
    for folder in ("data", "config"):
        target = os.path.join(APP_DIR, folder)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "README-COPY-OLD-DATA.txt"),
                  "w", encoding="utf-8") as fh:
            fh.write(
                "Copy the old HRAgent data/config contents into this folder.\n"
                "Keep master.key together with the old database.\n"
            )
    # 版本标记：更新器/安装器靠它判断已装版本、拒绝降级；也方便现场排障。
    from app.version import BUILD_CHANNEL
    with open(os.path.join(APP_DIR, "version.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "product": "HRAgent工作台",
            "version": version,
            "channel": BUILD_CHANNEL,
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    setup: str | None = None
    if not args.skip_installer:
        # 用 installer/make_setup.py 打单文件安装器：它装完会写 portable.flag，
        # 数据/配置留在安装目录内，和 v2.0.0 的 _internal\data|config 同构，
        # 老用户的 model.json / mailbox.json / *.secret 迁过来即用。
        # （repo_src/installer.py + HRAgentInstaller.spec 已废弃：它不写
        # portable.flag、也不迁移旧数据，装完配置目录是空的，模型和邮箱全部失效。）
        make_setup = os.path.join(WORKSPACE, "installer", "make_setup.py")
        if not os.path.isfile(make_setup):
            raise RuntimeError(f"缺少安装器构建脚本：{make_setup}")
        subprocess.run([sys.executable, make_setup], cwd=WORKSPACE, check=True)
        built = os.path.join(WORKSPACE, "release", "installer", "_dist",
                             f"HRAgent工作台-安装程序-v{version}.exe")
        if not os.path.isfile(built):
            raise RuntimeError(f"安装器构建失败：{built}")
        setup = os.path.join(RELEASES, f"HRAgent工作台-安装程序-v{version}.exe")
        shutil.copy2(built, setup)

    info = audit_program_files()
    portable = make_portable(version)
    update, _manifest = make_update(version)
    release_info(version, portable, update, setup, info)
    print(f"[prod] 完成：{portable}")
    print(f"[prod] 热更新：{update}")
    if setup:
        print(f"[prod] 安装包：{setup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
