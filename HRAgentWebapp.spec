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
           "tiers.json", "imap.secret", "embedding.json"):
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
if len(_units) != 10:
    raise SystemExit(f"[spec] 面试字典单位数 = {len(_units)}（应为 10）。"
                     "是不是拿到了源码内置的旧字典？确认 _package_cfg/interview.json 来自运行库。")
print(f"[spec] 面试字典：{len(_units)} 个单位（HR 维护版）")

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

# 邮箱授权码：mailbox.json 里**故意不存明文**（mailbox.py 读取时会剔除），
# 程序读口令的固定位置是 <base>/config/imap.secret。所以要让收件人开箱能收信，
# 只能把 imap.secret 一起分发。缺了就明说，别让人以为"配好了却收不到"。
_secret = os.path.join(_PACK_CFG, "imap.secret")
if os.path.isfile(_secret) and os.path.getsize(_secret) > 0:
    print("[spec] 邮箱授权码：随包分发（收件人可直接收简历）")
else:
    print("[spec] 邮箱授权码：**未随包分发** —— 收件人需自己在「系统配置」里"
          "填授权码才能收简历（跑 python _write_pack_secret.py 可随包带上）")

# 随包分发演示数据（v1.24.8 修：上一版只打了 config、没打 data →
# 收件人首次启动是**空库**，没有演示候选人、没有邮件模板、没有面试字典。
# HR 反馈"发给别人的包里没有面试邀请模板"，根因就是这里）。
# 由 _prepare_pack_data.py 生成：10 位虚构演示人 + 面试邀请模板 + 岗位 + 字典，
# 不含任何真实简历。
_PACK_DATA = os.path.join(ROOT, "_pack_data")
if os.path.isdir(_PACK_DATA):
    # 逐个子项写进datas，**不要**直接把 _pack_data 或 _pack_data/data 当源目录：
    #   (_PACK_DATA, "data")        → 落成 _internal/data/data/   （多一层，DB 读不到）
    #   (_PACK_DATA/data, ".")      → 内容全摊到 _internal/ 根、archive 丢失
    # datas 里的每一项是 (源, 目标目录)，目标目录不会自动补源目录名，所以
    # 要 data 就把源写成 `_pack_data/data/<名字>`、目标写 "data"。
    _pd_root = os.path.join(_PACK_DATA, "data")
    if not os.path.isfile(os.path.join(_pd_root, "workbench.db")):
        raise SystemExit("[spec] _pack_data/data/workbench.db 不存在 —— 先跑 _prepare_pack_data.py")

    # ---- 打包前强制清洗演示库（v1.24.9）----
    # **智能助手的对话记录一律不随包分发**：那些是我/开发过程中问出来的内容
    # （"查看目前情况"之类），对收件人毫无意义，还会让人以为这套系统已经被用过。
    # 清在这里而不是靠"记得手动删"——打包是重复动作，靠自觉迟早漏。
    import sqlite3
    _pack_db = os.path.join(_pd_root, "workbench.db")
    _con = sqlite3.connect(_pack_db)
    try:
        _tabs = {r[0] for r in _con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        # 1) 对话记录：问答正文、工具调用轨迹、token 成本都在这张表
        if "agent_runs" in _tabs:
            _n = _con.execute("SELECT count(*) FROM agent_runs").fetchone()[0]
            _con.execute("DELETE FROM agent_runs")
            print(f"[spec] 清洗演示库：已清除智能助手对话记录 {_n} 条")
        # 2) 审计日志里若有智能体运行痕迹也一并清掉（同样是我的测试痕迹）
        #    v1.24.18 修：原来只按entity='agent' / action LIKE '%agent%' 删，
        #    漏了 chat_clear（清空助手对话）—— 它entity 不是 agent，
        #    但留在库里就是一条"有人用过这系统"的痕迹，且没有对应记录看着更奇怪。
        if "audit_log" in _tabs:
            _n2 = _con.execute("SELECT count(*) FROM audit_log WHERE entity='agent' "
                               "OR action LIKE '%agent%' OR action LIKE 'chat%'").fetchone()[0]
            _con.execute("DELETE FROM audit_log WHERE entity='agent' "
                         "OR action LIKE '%agent%' OR action LIKE 'chat%'")
            if _n2:
                print(f"[spec] 清洗演示库：已清除智能助手审计痕迹 {_n2} 条")
        # 2b) 待确认提案同理：留着会出现"系统里有N 条待办但HR 从没提过"的错觉
        if "proposals" in _tabs:
            _n3 = _con.execute("SELECT count(*) FROM proposals").fetchone()[0]
            _con.execute("DELETE FROM proposals")
            if _n3:
                print(f"[spec] 清洗演示库：已清除遗留提案 {_n3} 条")
        # 3) 自增序列归零：让收件人库里 id 从 1 开始，不带着我的历史偏移
        for _t in ("agent_runs", "proposals"):
            if _t in _tabs:
                _con.execute("DELETE FROM sqlite_sequence WHERE name=?", (_t,))
        _con.commit()
        _con.execute("VACUUM")          # 顺带收紧文件（删过数据的页可复用）
        print("[spec]演示库已洁净：收件人首次打开没有任何历史对话")

        # ---- v1.24.18：给演示库预建语义索引（不然收件人打开就是空索引）----
        # embeddings 表现有 0 条 → semantic() 直接返回"索引为空"，界面里
        # 「语义找人」看起来像坏了。而演示库里明明有 10 位候选人。
        # 真实库里是导入时自动建索引，演示库不经过导入流程，所以得在这里补。
        # 失败不阻断打包：收件人导入自己的简历时会自动建。
        # ⚠️ **别把 import 写在 try 里**：上一版把 `from app import search`
        # 连同 sys.path 插入一起删掉了，异常被 except 降级成"跳过"，
        # 打包照样EXIT=0，日志里一行"跳过"看着像正常——
        # **降级会掩盖手误**，这正是 try/except 用在这里的代价。
        # 所以导入放在 try 之前，让导入失败直接炸出来。
        import sys as _sys
        if SRC not in _sys.path:
            _sys.path.insert(0, SRC)
        from app import search as _search

        # 关键：**不能直接用 load_cfg()** —— 它按 BASE 读的是工作区的 config/，
        # 而真正随包分发的是 _package_cfg/。两处配置可能不一致，
        # 用工作区那份建出来的向量，收件人那边模型对不上 → 分数全是噪音。
        # 这里显式读 _package_cfg，与分发内容严格同源。
        import json as _json
        _ecfg = {}
        for _fn in ("embedding.json", "secrets.json"):
            _fp = os.path.join(_PACK_CFG, _fn)
            if os.path.isfile(_fp):
                try:
                    with open(_fp, encoding="utf-8") as _fh:
                        _ecfg.update(_json.load(_fh))
                except (ValueError, OSError):
                    pass

        if _ecfg.get("base_url") and _ecfg.get("api_key"):
            try:
                _r = _search.build_index(_pack_db, cfg=_ecfg, force=True)
                _n = _r.get("indexed") or 0
                print("[spec] 演示库语义索引：%s，模型 %s，%d 条"
                      % ("已建立" if _n else "**未建立**", _r.get("model"), _n))
                if not _n:
                    print("[spec] ⚠️ 索引为 0 —— 收件人首次语义检索会是空的，"
                          "先查 embedding 服务是否可达")
            except Exception as _e:
                print("[spec] 演示库语义索引：**失败**（%s: %s）"
                      % (type(_e).__name__, _e))
        else:
            print("[spec] 演示库语义索引：跳过（_package_cfg 里没有 base_url/api_key）")
    finally:
        _con.close()

    for _name in os.listdir(_pd_root):
        _p = os.path.join(_pd_root, _name)
        if os.path.isdir(_p):
            #目录源会被摊平（实测 archive/*全散到 data/ 根），逐个孙项展开才能保层级
            for _sub in os.listdir(_p):
                datas.append((os.path.join(_p, _sub), os.path.join("data", _name)))
        else:
            datas.append((_p, "data"))
    print("[spec] 随包分发演示数据：_pack_data/data/* → _internal/data")
else:
    raise SystemExit(
        "[spec] 找不到 _pack_data —— 先跑 python _prepare_pack_data.py 生成演示数据。"
        "不要打空库：收件人打开会什么都没有。"
    )

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
