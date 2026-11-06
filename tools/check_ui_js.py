"""内联 JS 语法校验：把 app/ui.py 的页面 <script> 抽出来跑 node --check。

存在的理由（历史缺陷 #32）：ui.py 的 HTML 是 Python 三引号字符串，JS 里写一个
裸 `\n` 会被 Python 转义成真实换行，界面直接白屏，而 Python 语法检查照样通过。
所以每次改 ui.py 都必须单独校验内联 JS。

用法：
    python3 tools/check_ui_js.py          # 校验，失败退出码 1
"""
from __future__ import annotations


import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

#: v1.27：node 路径**按平台探测**，不再写死某台机器的绝对路径。
#: 原来硬编码 `/Users/hxy/...`（作者的 Mac），在 Windows 开发机上永远走
#: `SKIP 未找到 node` 分支 —— 语法校验形同虚设，而它恰恰是防"改 ui.py 白屏"
#: 的唯一一道自动关卡（本项目为此白屏过 4 次）。静默 SKIP 比报错更危险：
#: 人以为校验过了。
#: 优先级：环境变量 TP_NODE > PATH 里的 node > WorkBuddy 托管的 node。
_CANDIDATES = [
    os.environ.get("TP_NODE"),
    shutil.which("node"),
    os.path.expanduser("~/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"),
    os.path.expanduser("~/.workbuddy/binaries/node/versions/22.22.2-3/bin/node"),
]
NODE = next((p for p in _CANDIDATES if p and os.path.exists(p)), None)


def extract_scripts(html: str) -> list[str]:
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)


#: v1.29.2：**游离的 `async`**（本项目栽过 3 次，白屏 2 次）。
#: 成因都是"用 `function xxx(){` 当锚点做字符串替换"，而原文是
#: `async function xxx(){` —— 锚点匹配到了尾巴，替换后前缀 `async ` 被落下，
#: 变成 `async` 单独一行（后面还跟着注释），node --check **语法完全合法**
#: （`async` 被当成一个普通标识符表达式），要等**运行时**才报
#: `ReferenceError: async is not defined`，整页白。
#: 静态关卡抓不住"缺前缀"，但能抓住"async 后面没跟 function/括号/标识符"。
_LONE_ASYNC = re.compile(r"^\s*async\s*(?:/\*.*)?$|^\s*async\s+(?!function\b|[\w$([\"'`])")


def scan_lone_async(src: str) -> list[tuple[int, str]]:
    """返回 [(行号, 原文)]：把 `async` 当独立标识符用的行。"""
    out: list[tuple[int, str]] = []
    for n, line in enumerate(src.split("\n"), 1):
        # 跳过注释行与字符串里出现的 async
        st = line.strip()
        if st.startswith(("//", "*", "/*")):
            continue
        if _LONE_ASYNC.match(line):
            out.append((n, st[:100]))
    return out


#: v1.29.2：**HTML 注释里的反引号**会截断 JS 模板字符串（本项目栽过 4 次、4 次白屏）。
#: 成因：`_PAGE` 是 Python 三引号字符串，HTML 注释里写一个 ` 就等于给外层的
#: JS 模板字符串提前闭合 → node --check 报 "Invalid or unexpected token"，
#: 而 Python 语法检查照样通过。注释里写 `` `code` `` 看着无害，后果是整页白。
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def scan_backtick_in_html_comment(src: str) -> list[tuple[int, str]]:
    """返回 [(行号, 片段)]：HTML 注释里含反引号的地方。"""
    out: list[tuple[int, str]] = []
    for m in _HTML_COMMENT.finditer(src):
        if "`" not in m.group(0):
            continue
        line = src.count("\n", 0, m.start()) + 1
        frag = m.group(0).replace("\n", " ")[:90]
        out.append((line, frag))
    return out


def main() -> int:
    from app import ui

    html = ui._PAGE  # noqa: SLF001
    scripts = extract_scripts(html)
    if not scripts:
        print("FAIL 页面里没有找到 <script> 块")
        return 1

    if not NODE:
        # v1.27：**找不到 node 要报错退出，不能 SKIP**。
        # 这道校验是防白屏的唯一自动关卡，静默跳过等于把门拆了 ——
        # 人会以为"改完 ui.py 校验过了"，而实际上什么都没检查。
        print("FAIL 找不到 node，无法校验内联 JS。")
        print("      装一个 node 后重试，或用 TP_NODE 环境变量指定路径：")
        print("      TP_NODE=/path/to/node python tools/check_ui_js.py")
        return 1

    bad = 0
    for i, src in enumerate(scripts, 1):
        # v1.29.2：先查游离的 async（node --check 抓不到，只能静态查）
        # v1.29.2：HTML 注释里的反引号（与游离 async 同类，node --check 抓的那类）
        ticks = scan_backtick_in_html_comment(src)
        if ticks:
            bad += 1
            print(f"FAIL <script> 第 {i} 块：{len(ticks)} 处 HTML 注释里含**反引号**")
            print("      症状：node 报 Invalid or unexpected token → 整页白屏。")
            print("      成因：反引号提前截断了外层 JS 模板字符串（_PAGE 是 Python 三引号串）。")
            for n, t in ticks[:5]:
                print(f"        第 {n} 行: {t}")
        lone = scan_lone_async(src)
        if lone:
            bad += 1
            print(f"FAIL <script> 第 {i} 块：发现 {len(lone)} 处**游离的 async**")
            print("      症状：运行时 ReferenceError: async is not defined → 整页白屏。")
            print("      成因：字符串替换把 `async function f(){` 的 async 前缀落下了。")
            for n, t in lone[:5]:
                print(f"        第 {n} 行: {t}")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(src)
            path = f.name
        try:
            p = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
            if p.returncode != 0:
                bad += 1
                print(f"FAIL <script> 第 {i} 块（{len(src)} 字符）语法错误：")
                print(p.stderr.strip()[:2000])
            elif not lone:
                print(f"OK   <script> 第 {i} 块（{len(src)} 字符）")
        finally:
            os.unlink(path)

    # 说明：历史缺陷 #32（Python 三引号里的裸 \n 变成真实换行，导致 JS 字符串
    # 字面量跨行、界面白屏）由 node --check 直接报 "Invalid or unexpected token"，
    # 不需要另加启发式规则——试过用正则找"行尾未闭合单引号"，误报太多（正常代码
    # 的行末引号也会命中），已移除。

    if bad:
        print(f"\n结果：{bad} 个 script 块语法错误")
        return 1
    print(f"\n结果：{len(scripts)} 个 script 块语法全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
