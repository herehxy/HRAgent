# -*- mode: python ; coding: utf-8 -*-
"""HRAgent 单目录打包 spec。

产物：dist/HRAgent工作台/HRAgent工作台.exe（onedir）
数据落点：exe 同目录的 _internal/data 与 _internal/config。
"""
import os

# collect_all 不在 PyInstaller 注入的 spec 全局里，必须显式导入
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(SPECPATH)          # HRAgent 工作区根
SRC = os.path.join(ROOT, "repo_src")      # 源码目录

datas = [
    (os.path.join(SRC, "config", "universities.json"), "config"),
    (os.path.join(SRC, "config", "majors.json"), "config"),
    (os.path.join(SRC, "config", "ontology.json"), "config"),
    (os.path.join(SRC, "config", "jd.json"), "config"),
    # 注意：tiers.json / interview.json / model.json **不在这里**——
    # HR 在界面里改过它们（单位删到10 个、调了 A/B/C 阈值、换了模型），
    # 要以运行库那份为准，见下方 _PACK_CFG 段。
    # 原因（实测踩过）：PyInstaller 同名 datas 取**第一条**，不是后者覆盖前者，
    # 所以"从运行库追加一条"根本盖不住源码内置那份，必须从 datas 里移除。
    # embedding.json 同 tiers/interview：已从 datas 移除，改由 _PACK_CFG 段分发。
    # 原因同上——HR 换过 embedding 服务（现为百炼 qwen3.7-text-embedding-flash），
    # 源码内置那份指向 127.0.0.1:11434 的 Ollama，留着会盖住运行库配置。
    (os.path.join(SRC, "config", "domains"), "config/domains"),
    (os.path.join(SRC, "config", "mailbox.example.json"), "config"),
    # 托盘图标：256px PNG（清晰）；exe 窗口图标用 app.ico
    (os.path.join(ROOT, "icon_tray.png"), "."),
    (os.path.join(ROOT, "app.ico"), "."),
]

# ---- v1.24.6：邮件与模型配置随包分发（HR 明确要求"保留我的邮件和模型配置"）----
# ⚠️ 安全提醒（打包时会打在控制台里，别删）：
#    这两项是**明文凭据** —— 邮箱授权码 + 大模型 API Key。
#    拿到这个包的人可以用你的邮箱发信、用你的模型额度。
#    只发给你自己/可信的人；对外分发前请把这两个文件清空。
_PACK_CFG = os.path.join(ROOT, "_package_cfg")
if not os.path.isdir(_PACK_CFG):
    raise SystemExit("[spec] 找不到 _package_cfg —— 凭据与字典都要从这里分发，"
                     "先跑 python _collect_pack_config.py")
# interview.json 也从这里走：源码内置的是旧字典（15 个单位），
# 而 HR 运行库里是他自己维护的（10 个单位）→ 要"以用户设置的为准"就得从库里取。
for _f in ("mailbox.json", "secrets.json", "model.json", "interview.json",
           "tiers.json", "imap.secret", "embedding.json", "stages.json"):
    _src = os.path.join(_PACK_CFG, _f)
    if os.path.exists(_src):
        # 目标写 "config" 而不是 "_internal/config"：onedir 打包时 PyInstaller
        # 会自动把 datas 放进 `_internal/`，写全路径会多出一层
        # `_internal/_internal/config/`（实测踩过：配置"打包成功"却读不到）。
        datas.append((_src, "config"))
        print(f"[spec] 随包分发配置：{_f}")

# 硬断言：面试字典必须是 HR 自己维护那份（10 个单位），不是源码内置的 15 个
import json                                                  # noqa: E402
_dict = os.path.join(_PACK_CFG, "interview.json")
if not os.path.isfile(_dict):
    raise SystemExit("[spec] 缺 _package_cfg/interview.json —— 写邮件页下拉框会全空")
_units = json.load(open(_dict, encoding="utf-8")).get("units", [])
# ⚠️ 原来硬断言 ==10（当时 HR 维护的是 10 个单位）。单位数量是业务方随时增删的
# （2026-10-16 已是 13 个），写死数字会让"重新采集后打包直接中止"。这里改成
# 非空校验 + 打印数量：真正的保护是"必须来自采集步骤"（spec 的 datas 只认这份文件，
# 没有它下面 interview 字典断言就会炸）。
if not _units:
    raise SystemExit("[spec] _package_cfg/interview.json 没有单位 —— "
                     "先跑 python _collect_pack_config.py 从运行库采集")
print(f"[spec] 面试字典：{len(_units)} 个单位（运行库采集版）")

# 同理，档位规则也必须来自运行库（HR 调过 A/B/C 阈值）。
# 这条断言是被坑出来的：漏了 tiers.json，包里就只有源码内置那份，
# 而源码那份没有 thresholds → A/B/C 全按默认阈值判档，HR 调过的档位失效。
_tiers_src = os.path.join(_PACK_CFG, "tiers.json")
if not os.path.isfile(_tiers_src):
    raise SystemExit("[spec] 缺 _package_cfg/tiers.json —— HR 调过的 A/B/C 阈值会丢，"
                     "先跑 python _collect_pack_config.py")
if "thresholds" not in json.load(open(_tiers_src, encoding="utf-8")):
    raise SystemExit("[spec] _package_cfg/tiers.json 里没有 thresholds —— "
                     "拿到的不是运行库那份（HR 维护版）")
print("[spec] 档位规则：含 HR 自定义 thresholds")

# 反向断言：PII 主密钥绝不能进包。
# master.key 是 PII 加密密钥，进了包就等于"收件人拿到的演示数据用的是你的密钥"；
# 更糟的是将来若改成复用包内那份，HR 自己库里的手机号/邮箱将被别人的数据混淆。
# 现在是白名单分发（datas逐条列出），master.key 根本没被打进去——
# 但这份列表随时可能被人"顺手补一行"，所以显式拦一道。
_SRC_KEY = os.path.join(SRC, "config", "master.key")
for _i, (_s, _d) in enumerate(datas):
    if os.path.isfile(_s) and os.path.basename(_s) == "master.key" \
            and os.path.abspath(_s) != os.path.abspath(_SRC_KEY):
        raise SystemExit(f"[spec] datas 第 {_i+1} 项把 master.key 打进包了：{_s}")
print("[spec] PII 主密钥：未随包分发（仅演示数据自带自己的密钥）")

# v2.2.5：**凭据泄漏硬守卫**（用户 2026-10-22 决定："清空，不随包分发"）。
# 背景：`_package_cfg/` 是从运行库采集的，一旦开发库里填过真凭据，
# 采集脚本会把它一并带进来 → **真实邮箱授权码 + 大模型 API Key 被打进交付包**。
# 拿到包的人可以用用户的邮箱收简历、用他的模型额度。本次实测确实发生了。
# 所以这里不是"打印一句提醒"，而是**命中就中止打包**（EXIT≠0，别产出包）。
import re as _re                                              # noqa: E402

#: 私有 MaaS 专属端点（百炼 ws-xxxx）与 key 前缀 —— 出现即视为"带真凭据"
_CRED_PATTERNS = (
    (r"sk-ws-[A-Za-z0-9._\-]{10,}", "大模型 API Key（sk-ws-…）"),
    (r"ws-[a-z0-9]{14,}\.", "阿里云百炼专属端点（ws-xxxx.…）"),
    (r"https://[a-z0-9]{8,}-[a-z0-9]{8,}\.cn-beijing", "专属 MaaS 端点"),
)
_cred_hits = []
for _i, (_s, _d) in enumerate(datas):
    if not os.path.isfile(_s):
        continue
    try:
        _txt = open(_s, encoding="utf-8", errors="ignore").read()
    except OSError:
        continue
    for _pat, _label in _CRED_PATTERNS:
        if _re.search(_pat, _txt):
            _cred_hits.append(f"第 {_i+1} 项 {os.path.basename(_s)} —— {_label}")
if _cred_hits:
    raise SystemExit(
        "[spec] 拒绝打包：分发配置里带真实凭据\n"
        + "\n".join("         · " + h for h in _cred_hits)
        + "\n         清空方法：_package_cfg/secrets.json 的 api_key 置空、"
          "删除 _package_cfg/imap.secret、\n"
          "         model.json / embedding.json 的 base_url 换成中性端点。"
          "真值备份在 _vault/（该目录不进包）。")
print("[spec] 凭据检查：分发配置中无 API Key / 专属端点")

# 邮箱授权码：v2.2.5 起**默认不分发**（用户决定），收件人需自行在「系统配置」填。
# 若确实要开箱即收信，可跑 python _write_pack_secret.py 生成 _package_cfg/imap.secret。
_secret = os.path.join(_PACK_CFG, "imap.secret")
if os.path.isfile(_secret) and os.path.getsize(_secret) > 0:
    raise SystemExit(
        "[spec] 拒绝打包：_package_cfg/imap.secret 存在且非空 —— 邮箱授权码"
        "不能随包分发。\n         请删除该文件（真值备份在 _vault/），"
        "或改用收件人自填的方案。")
print("[spec] 邮箱授权码：未随包分发 —— 收件人需在「系统配置」自行填授权码")

# v1.29：**发信**口令（config/smtp.secret）的可见性提示。
# 这个文件刻意放在白名单**之外**（上面那份 datas 列表里没有它）——
# 因为收发信已分离，收件人完全可能是另一个账号，"随包带上发信口令"
# 等于把 HR 企业邮箱的发信能力一起发出去。这是有意收紧，不是漏了。
# 但**必须把结果打出来**：否则收件人拿到包、点发信才看到
# 「还没保存发信授权码」，而打包人以为自己已经配好了。
# ⚠️ 另外提醒：read_smtp_secret() 在 smtp.secret 缺失时会**回退读 imap.secret**，
#    所以收件人若与发件人同号，能靠回退直接工作；异号则必须自己填。
_smtp_secret = os.path.join(_PACK_CFG, "smtp.secret")
if os.path.isfile(_smtp_secret) and os.path.getsize(_smtp_secret) > 0:
    print("[spec] ⚠️ 发信口令：_package_cfg/smtp.secret 存在，但**不在 datas 白名单里**，"
          "不会被分发。若要连发信一起给出去，需显式把 smtp.secret 加进上面的列表"
          "（那等于交出你的企业邮箱发信能力，对外分发前请三思）。")
else:
    print("[spec] 发信口令：未随包分发 —— 收件人需在「系统配置 → 发信配置」自填"
          "客户端专用密码；若收件人与发件人是同一账号、同一口令，"
          "程序会回退读 imap.secret 而直接可用。")

# ===== 不随包分发任何数据（2026-10-26 业务方要求）=========================
# 交付包只更新**程序与前端**；数据与配置一律用用户原本的：
#   · 用户的库 / 简历 / 配置住在 %LOCALAPPDATA%\HRAgentData\ —— 安装与更新都不碰
#   · 老用户升级 = 安装器把他**自己的旧库**迁移过来（不是包里的演示库）
#   · 全新安装 = **空库**，HR 自己导入简历
#
# 为什么把原来的 _pack_data → _internal/seed 注入整段删掉：
#   启动器会把 `_internal/seed` 当作一个"旧版数据根"自动迁移到用户目录，
#   于是**任何人第一次打开都先看到 10 个虚构演示人** —— 分不清那是自己的数据还是假的，
#   更糟的是它会让"迁移失败"看起来像成功了（库里确实有人，只是不是他的）。
#   去掉之后：扫描命中不到任何种子 → 全新安装就是真空库，语义变干净。
#
# ⚠️ `_pack_data/` 仍保留在源码树里（本地开发造数据用），只是不进包。
#    需要临时打一个"带演示数据"的内部包时，用 git 回退这一段的旧写法即可。
_PACK_DATA = os.path.join(ROOT, "_pack_data")
print("[spec] 演示数据：**不注入** —— 包内无 workbench.db / 无 archive（数据一律用用户原本的）")

hiddenimports = [
    # uvicorn 运行时用 importlib 动态选择循环/协议实现，必须显式列出
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    # 文档解析（函数内延迟导入，显式声明更稳）
    "pymupdf",
    "markitdown",
    # app 子包保险项
    "app", "app.server", "app.agent", "app.agent.loop", "app.agent.tools",
    "app.agent.llm", "app.pipeline",
    # 任务栏托盘（pystray 按平台动态选后端，静态分析扫不到）
    "pystray", "pystray._win32", "pystray._util",
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageTk",
    # OCR（v1.13.8/v1.13.9：纸质扫描件简历靠它；RapidOCR = PP-OCRv3 的 ONNX 版，
    # 模型文件随包分发 ~13MB，不联网、不另起服务）
    "rapidocr_onnxruntime", "onnxruntime", "cv2",
    # rapidocr 还要读 config.yaml（PyYAML）、依赖 six/numpy——漏任何一个 import 就失败
    "yaml", "six", "numpy",
]
# OCR 模型文件（rapidocr_onnxruntime 自带 PP-OCRv3 检测+识别+方向分类）
# collect_all 返回 (datas, binaries, hiddenimports)，模型文件在 [0]
# OCR 依赖：rapidocr 的模型数据 + cv2 + **onnxruntime 的动态库**
# （只收数据文件不够：onnxruntime 的 .pyd/.dll 是 binaries，不收进来 import 就失败）
datas = datas + collect_all("rapidocr_onnxruntime")[0] + collect_all("cv2")[0]
# 这些都是带 C 扩展的包：**只写 hiddenimports 不会把 .pyd/.dll 带进来**，
# 打包后 import 会报 No module named（实测卡在 numpy）。
binaries = (collect_all("onnxruntime")[1] + collect_all("numpy")[1]
            + collect_all("cv2")[1] + collect_all("pyclipper")[1]
            + collect_all("shapely")[1])
hiddenimports = (hiddenimports + collect_all("rapidocr_onnxruntime")[2]
                 + collect_all("onnxruntime")[2] + collect_all("numpy")[2])


a = Analysis(
    [os.path.join(SRC, "webapp_launcher.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tests", "tkinter", "matplotlib", "pandas", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HRAgent工作台",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # 无控制台窗口：靠任务栏托盘控制，日志写 data/webapp.log
    icon=os.path.join(ROOT, "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HRAgent工作台",
)
