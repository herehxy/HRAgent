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
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

NODE = "/Users/hxy/.workbuddy/binaries/node/versions/22.22.2-3/bin/node"


def extract_scripts(html: str) -> list[str]:
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)


def main() -> int:
    from app import ui

    html = ui._PAGE  # noqa: SLF001
    scripts = extract_scripts(html)
    if not scripts:
        print("FAIL 页面里没有找到 <script> 块")
        return 1

    if not os.path.exists(NODE):
        print(f"SKIP 未找到 node：{NODE}")
        return 0

    bad = 0
    for i, src in enumerate(scripts, 1):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(src)
            path = f.name
        try:
            p = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
            if p.returncode != 0:
                bad += 1
                print(f"FAIL <script> 第 {i} 块（{len(src)} 字符）语法错误：")
                print(p.stderr.strip()[:2000])
            else:
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
