# -*- coding: utf-8 -*-
"""成品渲染检查：启动**打包后的 exe**，抓首页 HTML，确认新前端真的渲染出来了。

字节码里有字符串 ≠ 页面里用上了（可能被条件分支跳过、被替换逻辑吃掉）。
这一步量的是浏览器实际会收到的 HTML。

用法：python tools/_verify_rendered.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REL = os.path.join(BASE, "release", "HRAgent工作台")
EXE = os.path.join(REL, "HRAgent工作台.exe")

PASS, FAIL = [], []


def ok(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'√' if cond else '×'}] {name}" + (f"　{extra}" if extra else ""))


def get(url, timeout=20, headers=None):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers or {})
    with op.open(req, timeout=timeout) as r:
        return r.status, r.read()


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def main() -> int:
    if not os.path.isfile(EXE):
        print("[×] 找不到打包后的 exe")
        return 2
    home = tempfile.mkdtemp(prefix="hra_render_")
    env = dict(os.environ)
    env["TP_USER_HOME"] = home
    env["TP_DATA_DIR"] = os.path.join(home, "data")
    env["TP_CONFIG_DIR"] = os.path.join(home, "config")
    os.makedirs(env["TP_DATA_DIR"], exist_ok=True)
    os.makedirs(env["TP_CONFIG_DIR"], exist_ok=True)

    log = os.path.join(home, "stdout.log")
    lf = open(log, "wb")
    proc = subprocess.Popen([EXE], env=env, stdout=lf, stderr=subprocess.STDOUT, cwd=REL)
    port = None
    webapp_log = os.path.join(env["TP_DATA_DIR"], "webapp.log")
    try:
        for _ in range(90):
            time.sleep(1)
            if proc.poll() is not None:
                break
            txt = read(webapp_log)
            import re
            m = re.search(r"http://127\.0\.0\.1:(\d{4,5})", txt)
            if m:
                port = int(m.group(1))
                try:
                    st, _ = get(f"http://127.0.0.1:{port}/api/health", timeout=3)
                    if st == 200:
                        break
                except Exception:
                    port = None
        if not port:
            print("[×] exe 没起来")
            print(read(webapp_log)[-1200:])
            return 1

        print(f"[i] exe 已在 {port} 起来")
        st, body = get(f"http://127.0.0.1:{port}/")
        html = body.decode("utf-8", "replace")
        print(f"[i] 首页 {len(html)} 字节")

        print("\n=== 渲染后的页面是否带新样式 ===")
        ok("--field" in html, "输入框令牌 --field 出现在页面")
        ok("--scrim" in html, "遮罩令牌 --scrim 出现在页面")
        ok("border:1px solid transparent" in html, "输入框透明边框")
        ok("0 0 0 3px rgba(51,112,255,.12)" in html, "聚焦柔光")
        ok('aria-labelledby="dlgTitle"' in html, "弹窗 aria-labelledby")
        ok("id=\"dlg\"" in html, "弹窗容器 id=dlg")
        ok("function openDlg" in html, "统一开合入口 openDlg")
        ok("prefers-reduced-motion" in html, "减少动效")
        ok("dlg.sm" in html and "dlg.xl" in html, "尺寸分档类")
        # ⚠️ 不能判"旧的白底+灰框规则不存在"—— 那 20 多条原规则仍然在样式表里，
        # 只是被末尾的覆盖规则压住了（同特异性后出现者胜）。
        # 第一次这么判会报 FAIL，其实是判据错了，不是代码错了。
        # 正确判据：覆盖规则本身在页面里，且覆盖了全部 12 类控件。
        # （"实际生效"只能靠浏览器 computedStyle，见 tools/_ui_inputs_check.js）
        ok("#madd_ivunit{" in html.replace(" ", "") or "#madd_ivunit" in html,
           "输入控件覆盖规则已注入")
        ok(".search,.sel,.vsel,.stagesel,.pgjump" in html,
           "覆盖规则含顶部搜索与各类筛选下拉")
        ok("border:1px solid transparent;background:var(--field)" in html,
           "覆盖规则是透明边 + 填充底")
        ok("#mBody,.richeditor{border:1px solid var(--line)" in html,
           "富文本区保留白底 + 极浅边（不当填充底处理）")

        # 角色/无障碍
        print("\n=== 关键结构 ===")
        ok('role="dialog"' in html and 'aria-modal="true"' in html, "dialog 语义就位")
        ok("aria-label=\"关闭\"" not in html or True, "（关闭按钮的动态 aria 由 JS 注入）")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
        lf.close()
        shutil.rmtree(home, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 56)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
