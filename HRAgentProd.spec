# -*- mode: python ; coding: utf-8 -*-
"""Production onedir spec: program resources only, never user data or secrets."""
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(SPECPATH)
SRC = os.path.join(ROOT, "repo_src") if os.path.isdir(
    os.path.join(ROOT, "repo_src")) else ROOT
WORKSPACE = os.path.dirname(ROOT)
CONFIG_TEMPLATE = os.path.join(ROOT, "build_prod_assets", "config_template")
if not os.path.isdir(CONFIG_TEMPLATE):
    raise SystemExit("[prod-spec] 缺少 build_prod_assets/config_template，先运行 build_prod.py")

datas = [
    (CONFIG_TEMPLATE, "config_template"),
    (os.path.join(WORKSPACE, "app.ico"), "."),
    (os.path.join(WORKSPACE, "icon_tray.png"), "."),
    # 登录页背景图（v3.1.3）：放在 app/ 下，server 的 /login-bg.jpg 从这里读。
    # 缺了也不致命（CSS 有兜底渐变），但界面上会少一张企业照片。
    (os.path.join(ROOT, "app", "login_bg.jpg"), os.path.join("app")),
    (os.path.join(ROOT, ".runtime_local", "certifi", "cacert.pem"), "."),
]

datas += collect_all("certifi")[0]

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols", "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    "pymupdf", "markitdown", "mammoth", "magika", "olefile",
    "app", "app.server", "app.profile_migrate", "app.version",
    "app.agent", "app.agent.loop", "app.agent.tools", "app.agent.llm",
    "app.pipeline",
    # v4.0.16：招聘进度（导入/导出走 openpyxl 的多表/合并能力）
    "app.recruit", "app.recruit_export",
    "openpyxl", "openpyxl.styles", "openpyxl.worksheet.properties",
    "pystray", "pystray._win32", "pystray._util",
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageTk",
    "rapidocr_onnxruntime", "onnxruntime", "cv2",
    "yaml", "six", "numpy",
]

datas += (
    collect_all("pymupdf")[0]
    + collect_all("fitz")[0]
    + collect_all("rapidocr_onnxruntime")[0]
    + collect_all("cv2")[0]
    + collect_all("shapely")[0]
    # MarkItDown 只是 PDF/DOCX 的第三回退；它依赖 magika 的 ONNX 模型数据，
    # 不 collect_all 的话打包版一构造就 FileNotFoundError（v5.0.0 的根因之一）。
    + collect_all("magika")[0]
    + collect_all("markitdown")[0]
    + collect_all("mammoth")[0]
)
binaries = (
    collect_all("pymupdf")[1]
    + collect_all("fitz")[1]
    + collect_all("onnxruntime")[1]
    + collect_all("numpy")[1]
    + collect_all("cv2")[1]
    + collect_all("pyclipper")[1]
    + collect_all("shapely")[1]
)
hiddenimports += (
    collect_all("pymupdf")[2]
    + collect_all("fitz")[2]
    + collect_all("rapidocr_onnxruntime")[2]
    + collect_all("onnxruntime")[2]
    + collect_all("numpy")[2]
    + collect_all("cv2")[2]
    + collect_all("pyclipper")[2]
    + collect_all("shapely")[2]
    + collect_all("magika")[2]
    + collect_all("markitdown")[2]
    + collect_all("mammoth")[2]
)

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
    console=False,
    icon=os.path.join(WORKSPACE, "app.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HRAgent工作台",
)
