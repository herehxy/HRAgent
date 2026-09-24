"""界面交互态验收：用 Chrome 调试协议驱动真实页面，把"要点击才看得到"的部分也截下来。

为什么需要它：`chrome --headless --screenshot <url>` 只能截静态首屏，
而这次改动的关键点（检索结果里的联系方式、JD 编辑弹层）都要先点一下才出现。
本脚本用 CDP（Runtime.evaluate + Page.captureScreenshot）驱动页面，
截图落盘到 out/ui-screenshots/，供人工核对。

用法：
    python tests/ui_probe.py --url http://127.0.0.1:8756 --out out/ui-screenshots

注意：这里不 mock 任何数据，页面走的都是真实接口与真实库。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333


# ----------------------------------------------------------------- CDP 客户端
class Chrome:
    def __init__(self, port: int = PORT, width: int = 1500, height: int = 1500,
                 spawn: bool = False):
        self.port = port
        self.width = width
        self.height = height
        self.spawn = spawn
        self.proc: subprocess.Popen | None = None
        self.ws = None
        self._id = 0
        self._profile = f"/tmp/wb-ui-probe-{os.getpid()}"

    def _tabs(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as r:
            return json.loads(r.read().decode())

    def start(self) -> str:
        """连接到已运行的 Chrome 调试端口。

        默认**不自己拉起 Chrome**：本执行环境里从 Python 子进程启动 Chrome 会失败
        （沙箱限制），但用 bash 直接起是好的。所以约定是——
        先由外部起好：
            "<Chrome>" --headless=new --remote-debugging-port=9333 \
                        --user-data-dir=/tmp/wb-cdp about:blank &
        这里只负责连上去。spawn=True 时才尝试自己拉起（本地开发环境可用）。
        """
        if self.spawn:
            os.makedirs(self._profile, exist_ok=True)
            self.proc = subprocess.Popen(
                [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                 "--no-proxy-server", "--no-first-run", "--disable-extensions",
                 f"--remote-debugging-port={self.port}",
                 f"--user-data-dir={self._profile}",
                 f"--window-size={self.width},{self.height}",
                 "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        last = None
        for _ in range(40):
            try:
                page = [t for t in self._tabs() if t.get("type") == "page"]
                if page:
                    self._connect(page[0]["webSocketDebuggerUrl"])
                    return "connected"
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(0.5)
        raise RuntimeError(
            f"连不上 Chrome 调试端口 {self.port}：{last}\n"
            f"请先用 bash 起一个：\n  \"{CHROME}\" --headless=new --disable-gpu \\\n"
            f"    --no-proxy-server --no-first-run --user-data-dir=/tmp/wb-cdp \\\n"
            f"    --remote-debugging-port={self.port} about:blank &")

    def _connect(self, ws_url: str) -> None:
        import websocket  # websocket-client

        # ① 不走环境代理（否则连本机 9333 也会被转发出去）
        # ② suppress_origin：Chrome 会以 "Origin 不被允许" 为由 403 掉带 Origin 的握手，
        #    要么给 Chrome 加 --remote-allow-origins，要么干脆不发 Origin（这里选后者）
        self.ws = websocket.create_connection(
            ws_url, timeout=30, http_proxy_host=None, http_proxy_port=None,
            proxy_type=None, suppress_origin=True,
        )

    def call(self, method: str, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败：{msg['error']}")
                return msg.get("result", {})

    # ---- 便捷方法
    def set_viewport(self, width: int | None = None, height: int | None = None) -> None:
        """固定视口宽度。

        必须显式设置：调试端口启动的 Chrome 默认视口只有 800×600，
        窄视口下表格会被挤成一列一字，看起来像"列宽坏了"，其实是截图环境的问题。
        """
        self.call("Emulation.setDeviceMetricsOverride",
                  width=width or self.width, height=height or self.height,
                  deviceScaleFactor=1, mobile=False)

    def goto(self, url: str, settle: float = 1.6) -> None:
        self.call("Page.enable")
        # 必须禁缓存：单页应用整份 HTML/JS 是一张文档，改完后端再跑验收时，
        # 浏览器会直接吃磁盘缓存里的旧 JS，于是"验收通过/失败"说的都是上一版代码。
        # 这里第一次就踩到了——服务端已下发新 JS，页面里跑的还是旧的。
        self.call("Network.enable")
        self.call("Network.setCacheDisabled", cacheDisabled=True)
        # **但只设 setCacheDisabled 不够**（实测）：它管得住子资源，管不住"文档本身"。
        # 单页应用的整份 HTML/JS 正是那个文档，于是"已下发新前端、页面里仍是旧函数体"
        # 再次发生——验收静默地验了上一版代码，而且断言全绿。
        # 所以在 URL 上再加一串每次都不同的查询参数，从根上绕开文档级强缓存。
        self.call("Page.navigate", url=self._bust(url))
        time.sleep(settle)

    @staticmethod
    def _bust(url: str) -> str:
        """给文档 URL 加一次性查询参数（保留 #hash），强制真取网络副本。

        `http://h/#org` → `http://h/?_tp=1727...#org`
        """
        head, sep, frag = url.partition("#")
        joiner = "&" if "?" in head else "?"
        return f"{head}{joiner}_tp={int(time.time() * 1000)}{sep}{frag}"

    def eval(self, expr: str, await_promise: bool = False):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError(f"页面 JS 报错：{d.get('text')} {d.get('exception', {}).get('description', '')}")
        return r.get("result", {}).get("value")

    def shot(self, path: str) -> str:
        r = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        return path

    def close(self) -> None:
        """只断开连接。Chrome 由外部启动，就不在这里杀掉（可能还有别的用途）。"""
        try:
            if self.ws:
                self.ws.close()
        finally:
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.proc.kill()


# ----------------------------------------------------------------- 验收场景
def _disk_ui_build() -> str | None:
    """从磁盘新导入 `app.ui`，取它算出的版本戳。

    验收脚本与后端是两个进程：脚本这边新导入 → 反映**磁盘上的代码**；
    后端那边是启动时导入的常量 → 反映**正在运行的代码**。两者不一致，
    就说明前端改了但服务没重启（uvicorn 不热加载），此时验的是旧代码。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.ui import ui_build  # noqa: PLC0415
        return ui_build()
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8756")
    ap.add_argument("--out", default="out/ui-screenshots")
    args = ap.parse_args()

    # 执行环境注入了 HTTP 代理，本机调试端口（Chrome 9333 / 工作台 8756）必须直连，
    # 否则会被转发到代理上，表现为"连不上"，看起来像服务没起来。
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY",
              "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"

    c = Chrome()
    ok = True
    try:
        c.start()
        c.set_viewport()

        # ⓪ 自证：页面里跑的前端 == 服务端当前提供的 == 磁盘上最新的。
        # 这一步不能省。单页应用整份 HTML/JS 就是一张文档，有两条路会让验收**静默地验上一版代码并全绿**：
        #   ① 浏览器把文档缓存了（`Network.setCacheDisabled` 管不住文档本身，实测踩到）；
        #   ② 后端改了 ui.py 但服务没重启（uvicorn 不热加载，改完前端必须重启）。
        # 两者都表现为"断言全过、但验的不是这版代码"，比断言失败更危险，所以显式断言版本戳。
        c.goto(f"{args.url}/")
        page_build = c.eval("(typeof UI_BUILD === 'string') ? UI_BUILD : null")
        srv_build = c.eval(
            "(async()=>{const r = await api('/api/meta'); return r.ui_build || null;})()",
            await_promise=True)
        disk_build = _disk_ui_build()
        fits = page_build and page_build == srv_build == disk_build
        print(f"  版本戳自证（页面{page_build} / 服务端{srv_build} / 磁盘{disk_build}）："
              f"{'一致' if fits else '不一致'}")
        if not fits:
            if srv_build != disk_build:
                print("    → 服务端跑的不是磁盘上的最新 `app/ui.py`：服务未重启（uvicorn 不热加载），"
                      "先重启再验收")
            elif page_build != srv_build:
                print("    → 浏览器拿到的不是服务端当前下发的文档：缓存没绕开，先清 CDP 的 profile 重试")
        ok = ok and bool(fits)

        # ① 检索 → 技能召回结果里的联系方式（要点击才出现）
        c.goto(f"{args.url}/#search")
        c.eval("document.getElementById('skInput').value = '钛合金,真空熔铸';"
               "document.getElementById('skMode').value = 'any';"
               "doSkillSearch();")
        time.sleep(1.8)
        html = c.eval("document.getElementById('skOut').innerHTML") or ""
        has_tel = "tel:" in html or "mailto:" in html
        print(f"  技能召回结果含可点拨号/发信链接：{has_tel}")
        ok = ok and has_tel
        c.shot(os.path.join(args.out, "10-检索-技能召回带联系方式.png"))

        # ② 检索 → 语义召回结果（表格列）
        c.eval("document.getElementById('semInput').value = '有难熔合金研发背景的博士';"
               "doSemSearch();")
        time.sleep(1.8)
        table = c.eval("document.querySelector('#semOut table') ? "
                       "document.querySelector('#semOut table').innerText : ''") or ""
        print(f"  语义召回表头含「联系方式」：{'联系方式' in table}")
        ok = ok and ("联系方式" in table)
        c.shot(os.path.join(args.out, "11-检索-语义召回带联系方式.png"))

        # ③ 岗位 JD 编辑弹层
        c.goto(f"{args.url}/#org")
        jid = c.eval(
            "(async()=>{const r=await api('/api/jobs');"
            "return (r.items&&r.items.length)?r.items[0].id:null;})()",
            await_promise=True)
        if jid:
            c.eval(f"editJd({jid});")
            time.sleep(1.2)
            modal_on = c.eval("document.getElementById('modal').classList.contains('on')")
            fields = c.eval("['ejMust','ejPref','ejEdu','ejYears','ejNote']"
                            ".every(i=>!!document.getElementById(i))")
            print(f"  JD 弹层打开：{modal_on}；字段齐全：{fields}")
            ok = ok and bool(modal_on) and bool(fields)
            c.shot(os.path.join(args.out, "12-岗位-JD编辑弹层.png"))
        else:
            print("  跳过 JD 弹层：库中没有岗位")
            ok = False

        # ③b 岗位管理页：2026-09 改版后**只保留岗位**（名称 + JD），部门概念整体移除。
        #    这条断言是"防回退"用的：岗位页若又长出部门列/部门表单，立刻报失败。
        c.goto(f"{args.url}/#org")
        time.sleep(1.4)
        org_html = c.eval("(document.getElementById('view').innerHTML||'')") or ""
        org_text = c.eval("(document.getElementById('view').innerText||'')") or ""
        no_dept_word = "部门" not in org_text
        no_dept_fn = not any(k in org_html for k in ("addDept", "toggleDept", "jobDept"))
        job_rows = c.eval(
            "(async()=>{const r=await api('/api/jobs');"
            "return (r.items||[]).map(x=>({t:x.title,d:(x.dept||'')}));})()",
            await_promise=True) or []
        n_jobs = len(job_rows)
        all_dept_empty = all((x.get("d") or "") == "" for x in job_rows)
        # 不写死"必须 4 个"（换库/加岗位都会误报），改为锁住**幻影岗位**这个真回归：
        # 此前 `cli.py ingest/mail` 与 `/api/meta` 会按 `config/jd.json` 的 role 自动建岗，
        # 于是库里凭空多出一个"默认岗位"，把真实岗位数带偏。
        phantom = [x["t"] for x in job_rows if x["t"] in ("默认岗位", "")]
        print(f"  岗位管理页：{n_jobs} 个岗位；页面无「部门」字样 {no_dept_word}；"
              f"无部门相关函数/字段 {no_dept_fn}；接口 dept 全为空 {all_dept_empty}；"
              f"无幻影岗位 {not phantom}")
        ok = ok and n_jobs > 0 and no_dept_word and no_dept_fn and all_dept_empty and not phantom

        # ③b2 重新分析：岗位列表要有入口，且默认是"预演"（先给差异、不写库）
        # 只看接口 200 会漏掉"功能做了但 HR 点不到"，所以断言打在 DOM 与弹层内容上。
        c.goto(f"{args.url}/#org")
        time.sleep(1.4)
        has_regrade_btn = c.eval("(document.getElementById('view').innerHTML||'')"
                                 ".includes('regradeJob(')")
        print(f"  岗位列表含「重新分析」按钮：{bool(has_regrade_btn)}")
        ok = ok and bool(has_regrade_btn)
        if jid:
            c.eval("(async()=>{closeModal();await regradeJob("+str(jid)+");})()",
                   await_promise=True)
            time.sleep(2.2)
            rg = c.eval("document.getElementById('mBody').innerText") or ""
            has_preview_btn = c.eval("(document.getElementById('mBody').innerHTML||'')"
                                     ".includes('regradeJob(')")
            print(f"  重算弹层给出差异：{'建议档位' in rg or '共' in rg}；"
                  f"含「应用重算结果」入口：{bool(has_preview_btn)}")
            ok = ok and ('预演' in rg or '尚未写入' in rg or '共' in rg)
            c.shot(os.path.join(args.out, "12b-岗位-重新分析差异预览.png"))

            # ③c 应用重算结果：必须真的落库，不能只是把弹层文案换一下。
            # 判定口径用"闭环"：应用后再预演一次，"将变化"必须归零；
            # 库没写进去的话，第二次预演照样报同样的差异条数。
            # 无差异的库（真实库就是）没有这个按钮，跳过——所以这段是条件执行。
            if has_preview_btn:
                c.eval("(async()=>{await regradeJob(" + str(jid) + ",true);})()",
                       await_promise=True)
                time.sleep(2.2)
                after = c.eval("document.getElementById('mBody').innerText") or ""
                closed = c.eval(
                    "(async()=>{const r=await api('/api/jobs/" + str(jid) + "/regrade',"
                    "{method:'POST',body:JSON.stringify({apply:false})});"
                    "return {changed:r.changed, applied:r.applied};})()",
                    await_promise=True) or {}
                still_btn = c.eval("(document.getElementById('mBody').innerHTML||'')"
                                   ".includes('应用重算结果')")
                print(f"  应用后提示已落库：{'落库' in after}；"
                      f"再预演将变化：{closed.get('changed')}（应为 0）；"
                      f"应用入口已收起：{not bool(still_btn)}")
                ok = ok and ('落库' in after) and closed.get('changed') == 0 \
                    and not bool(still_btn)
                c.shot(os.path.join(args.out, "12c-岗位-重新分析已应用.png"))
                c.eval("closeModal();")
            else:
                print("  跳过「应用重算结果」：当前库该岗位重算后无差异（预演即空，按钮不渲染）")

        # ④ 完整档案里的联系方式与原件下载按钮
        c.goto(f"{args.url}/#pool")
        c.eval("showDetail(1);")
        time.sleep(1.8)
        detail = c.eval("document.getElementById('mBody').innerText") or ""
        has_dl = c.eval("document.getElementById('mBody').innerHTML.includes('openDoc(')")
        print(f"  完整档案含联系方式：{'联系方式' in detail}；含原件下载/预览按钮：{bool(has_dl)}；"
              f"含性别行：{'性别' in detail}")
        ok = ok and ("联系方式" in detail) and bool(has_dl) and ("性别" in detail)
        c.shot(os.path.join(args.out, "13-完整档案-联系方式与原件.png"))

        # ④b 弹层开着时切页：弹层必须自动收掉，否则整屏遮罩会盖住新页面，
        #     表现为"点了左侧导航没反应"（此前 go() 不关弹层，确实如此）
        c.eval("go('import');")
        time.sleep(1.2)
        modal_after_nav = c.eval("document.getElementById('modal').classList.contains('on')")
        print(f"  切页后弹层已自动关闭：{not modal_after_nav}")
        ok = ok and (not modal_after_nav)

        # ⑤ 导入与来源（交互态：点一次"看邮箱里有什么"）
        # 用轮询而不是固定等待：邮箱可能是真实 IMAP（握手要好几秒），
        # 写死 sleep 会把"只是慢"误判成"没结果"。
        c.goto(f"{args.url}/#import")
        c.eval("previewMail();")
        pv = ""
        for _ in range(24):
            time.sleep(0.5)
            pv = c.eval("document.getElementById('mailPreview').innerText") or ""
            if ("封" in pv) or ("失败" in pv) or ("无法" in pv):
                break
        print(f"  邮箱预览返回可读结果：{'封' in pv}")
        ok = ok and ("封" in pv)
        c.shot(os.path.join(args.out, "14-导入与来源-邮箱预览.png"))

        # ⑥ 来源文件清单：预览 / 下载 / 多选打包都要真的在页面上
        # 这几项此前只有后端、界面上没有入口——所以断言必须打在 **DOM** 上，
        # 只看接口返回 200 会漏掉"功能做了但 HR 点不到"这一类问题。
        c.goto(f"{args.url}/#import")
        time.sleep(1.6)
        rows = c.eval("document.querySelectorAll('.fileChk').length") or 0
        has_all = c.eval("!!document.getElementById('chkAll')")
        # 行内按钮（预览/下载/删除）是跟着文件行渲染的：来源文件夹当前有没有简历文件，
        # 是用户的使用状态、不是验收的输入（同"勾选 2 份"的教训）——
        # 目录为空时行内按钮天然不存在，只能跳过，不能算失败。
        has_pv_btn = c.eval("(document.getElementById('view').innerHTML||'')"
                            ".includes('openSourceFile(')")
        print(f"  文件清单 {rows} 行；行内预览/下载按钮：{bool(has_pv_btn) if rows else '（空目录，跳过）'}；"
              f"全选：{bool(has_all)}")
        ok = ok and bool(has_all) and (bool(has_pv_btn) if rows else True)

        # 多选 → 计数要跟着变；全选 → 计数等于总行数。
        # 勾几行不能写死：用户可能只放了 1 份简历（来源目录指向哪、放几份，是用户的使用状态，
        # 不是验收的输入）——所以按"min(2, 行数)"勾，断言也跟着行数走。
        if rows == 0:
            print("  来源清单为空（当前文件夹没有可导入文件），跳过勾选与打包断言")
        else:
            pick = min(2, rows)
            c.eval("(()=>{const b=document.querySelectorAll('.fileChk');"
                   "for(let i=0;i<" + str(pick) + ";i++){b[i].checked=true;"
                   "b[i].dispatchEvent(new Event('change'));}})()")
            time.sleep(0.4)
            sel2 = c.eval("(document.getElementById('selCount')||{}).innerText") or "0"
            c.eval("toggleAllFiles(true);")
            time.sleep(0.4)
            sel_all = c.eval("(document.getElementById('selCount')||{}).innerText") or "0"
            c.eval("toggleAllFiles(false);")
            print(f"  勾选 {pick} 份 → 计数 {sel2}；全选 → 计数 {sel_all}（共 {rows} 行）")
            ok = ok and sel2 == str(pick) and sel_all == str(rows)
            pick3 = min(3, rows)
            c.eval("(()=>{const b=document.querySelectorAll('.fileChk');"
                   "for(let i=0;i<" + str(pick3) + ";i++){b[i].checked=true;"
                   "b[i].dispatchEvent(new Event('change'));}})()")
            time.sleep(0.3)
            c.shot(os.path.join(args.out, "15-导入与来源-文件清单与多选.png"))

        # ⑦ 文件夹路径与邮箱账号：界面上要有可改的入口（不只是配置文件里能改）
        has_dir_input = c.eval("!!document.getElementById('srcDir')")
        has_save = c.eval("(document.getElementById('view').innerHTML||'')"
                          ".includes('saveSourceDir()')")
        mail_fields = c.eval("['ixMode','ixHost','ixUser','ixPass','ixPort','ixSsl','ixPreset']"
                             ".every(i=>!!document.getElementById(i))")
        print(f"  文件夹路径输入框：{bool(has_dir_input)}；保存按钮：{bool(has_save)}；"
              f"邮箱账号/服务器/端口/SSL/口令/服务商预设字段：{bool(mail_fields)}")
        ok = ok and bool(has_dir_input) and bool(has_save) and bool(mail_fields)
        c.shot(os.path.join(args.out, "16-导入与来源-文件夹与邮箱可改.png"))

        # ⑦b 来源文件删除入口 + 二次确认 + 回收目录
        # 只把"删除"做成后端能力、界面上没按钮，等于没做——所以断言落在 DOM 上。
        # 行内删除按钮随文件行渲染，空目录时跳过（同上：不把使用状态当验收输入）。
        has_del_btn = c.eval("(document.getElementById('view').innerHTML||'')"
                             ".includes('removeSourceFiles(')")
        has_batch_del = c.eval("(document.getElementById('view').innerHTML||'')"
                               ".includes('removeSelected()')")
        print(f"  来源清单行内删除按钮：{bool(has_del_btn) if rows else '（空目录，跳过）'}；"
              f"批量删除按钮：{bool(has_batch_del)}")
        ok = ok and bool(has_batch_del) and (bool(has_del_btn) if rows else True)
        # 二次确认必须真的弹出来：把 confirm 换成记录器，再触发一次删除。
        # 直接点会真的移走样例文件，所以这里只验证"确认框被调用过"。
        dlg = c.eval("(()=>{window.__cf=null;const o=window.confirm;"
                     "window.confirm=m=>{window.__cf=m;return false;};"
                     "try{removeSourceFiles(['__probe_not_exist__.pdf']);}"
                     "finally{setTimeout(()=>{window.confirm=o;},300);}"
                     "return true;})()")
        time.sleep(0.8)
        cf = c.eval("window.__cf") or ""
        print(f"  删除前弹出二次确认：{bool(cf)}（{'回收目录' in cf}）")
        ok = ok and bool(cf) and ("回收目录" in cf)
        c.shot(os.path.join(args.out, "16b-导入与来源-删除与回收目录.png"))

        # ⑦c 性别标签：人才库卡片上要看得到（有性别的人），开关关着时不出现筛选项
        c.goto(f"{args.url}/#pool")
        time.sleep(1.6)
        pool_html = c.eval("(document.getElementById('view').innerHTML||'')") or ""
        has_gender_chip = c.eval(
            "(async()=>{const r=await api('/api/candidates');"
            "return (r.items||[]).some(x=>(x.gender||'').trim()!=='');})()",
            await_promise=True)
        gf_off = c.eval(
            "(async()=>{const r=await api('/api/candidates?gender=%E5%A5%B3');"
            "return !!(r.gender_filter&&r.gender_filter.enabled===false);})()",
            await_promise=True)
        print(f"  库中有人带性别标签：{bool(has_gender_chip)}；性别筛选开关默认关闭（接口侧）：{bool(gf_off)}")
        ok = ok and bool(has_gender_chip) and bool(gf_off)
        c.shot(os.path.join(args.out, "18-人才库-性别标签.png"))

        # ⑦c-b 软归档闭环（v1.4，真实库、可逆）：人才库卡片要有「归档」按钮；
        # 归档后从人才库默认列表消失、进「归档」页；取消归档后恢复。
        # 软归档不删任何数据、且两次动作都写审计，所以在真实库上闭环是安全的。
        c.goto(f"{args.url}/#pool")
        time.sleep(1.6)
        pool_html = c.eval("(document.getElementById('view').innerHTML||'')") or ""
        has_arch_btn = "archiveCandidate(" in pool_html
        print(f"  人才库卡片含「归档」按钮：{bool(has_arch_btn)}")
        ok = ok and bool(has_arch_btn)
        target = c.eval(
            "(async()=>{const r=await api('/api/candidates');"
            "return (r.items&&r.items.length)?r.items[0].id:null;})()",
            await_promise=True)
        if target:
            c.eval("window.__origConfirm=window.confirm;window.confirm=()=>true;")
            c.eval(f"archiveCandidate({target},true);")
            gone = False
            for _ in range(16):
                time.sleep(0.4)
                gone = c.eval(
                    "(async()=>{const r=await api('/api/candidates');"
                    f"return !(r.items||[]).some(x=>x.id==={target});}})()",
                    await_promise=True)
                if gone:
                    break
            print(f"  归档后从人才库默认列表消失：{bool(gone)}")
            ok = ok and bool(gone)
            c.goto(f"{args.url}/#archive")
            time.sleep(1.8)
            in_arch = c.eval(
                "(async()=>{const r=await api('/api/candidates?archived=1');"
                f"return (r.items||[]).some(x=>x.id==={target});}})()",
                await_promise=True)
            arch_html = c.eval("(document.getElementById('view').innerHTML||'')") or ""
            has_unarch_btn = "archiveCandidate(" in arch_html
            print(f"  「归档」页能看到该档案：{bool(in_arch)}；含「取消归档」按钮：{bool(has_unarch_btn)}")
            ok = ok and bool(in_arch) and bool(has_unarch_btn)
            c.shot(os.path.join(args.out, "18b-归档页-已归档档案.png"))
            c.eval(f"archiveCandidate({target},false);")
            back = False
            for _ in range(16):
                time.sleep(0.4)
                back = c.eval(
                    "(async()=>{const r=await api('/api/candidates');"
                    f"return (r.items||[]).some(x=>x.id==={target});}})()",
                    await_promise=True)
                if back:
                    break
            print(f"  取消归档后恢复在人才库展示：{bool(back)}")
            ok = ok and bool(back)
            c.eval("if(window.__origConfirm)window.confirm=window.__origConfirm;")
        else:
            print("  跳过归档闭环：人才库为空")

        # ⑦c-c v1.5：批量归档入口、30 天彻底删除入口、建议岗位芯片、CSV 列口径。
        # 口径与后端自检 S6/S7 对齐：界面必须能被"点到"，而不是只在接口层存在。
        c.eval("if(window.__origConfirm)window.confirm=window.__origConfirm;")
        c.goto(f"{args.url}/#pool")
        time.sleep(1.6)
        pool2 = c.eval("(document.getElementById('view').innerHTML||'')") or ""
        has_batch_btn = ("归档勾选的人" in pool2) and ("archiveBatch(null,true)" in pool2)
        has_by_year = "按年份归档" in pool2 or "按年" in pool2
        pick_cnt = c.eval("document.querySelectorAll('.pickChk').length") or 0
        print(f"  人才库含批量归档入口：{bool(has_batch_btn)}；含按年归档：{bool(has_by_year)}；"
              f"勾选框数：{pick_cnt}")
        ok = ok and bool(has_batch_btn) and bool(has_by_year) and int(pick_cnt) > 0
        # 建议岗位（v1.5）：待指定投递要么给出建议芯片、要么如实说明零交集——两者都不出现才算失败
        has_sug_chip = "建议岗位：" in pool2
        has_assign_btn = "采纳建议岗位" in pool2
        has_no_hit = "均无交集" in pool2
        has_assign_fn = c.eval("typeof assignJob==='function'")
        print(f"  待指定投递带建议岗位：{bool(has_sug_chip)}；采纳按钮：{bool(has_assign_btn)}；"
              f"零交集如实说明：{bool(has_no_hit)}；采纳函数就绪：{bool(has_assign_fn)}")
        ok = ok and bool(has_assign_fn) and (bool(has_sug_chip) or bool(has_assign_btn) or bool(has_no_hit))

        # ⑦c-d 分页（v1.7.1）：人才库每页 10 人，接口在筛后全量上切片。
        # 两条兼容口径必须同时保住：
        #   ① 不带 page 参数仍返回全量（导出 CSV / 探针 / 智能体工具靠它，paging 为 null）；
        #   ② total / gender_facets 是**筛后全量**口径，不随页码变。
        # 断言不写死库里有几个人：total<=10 时只有一页，第 2 页断言按条件跳过。
        pg_api = c.eval(
            "(async()=>{const all=await api('/api/candidates');"
            "const p1=await api('/api/candidates?page=1&page_size=10');"
            "const p2=await api('/api/candidates?page=2&page_size=10');"
            "return {total:(all.items||[]).length, all_paging: all.paging,"
            " p1:p1.paging, p1_n:(p1.items||[]).length, p2_n:(p2.items||[]).length};})()",
            await_promise=True) or {}
        tot = int(pg_api.get("total") or 0)
        p1 = pg_api.get("p1") or {}
        exp_pages = max(1, (tot + 9) // 10)
        compat_ok = (pg_api.get("all_paging") is None) and (tot > 0)
        p1_ok = bool(p1) and p1.get("total") == tot and p1.get("total_pages") == exp_pages \
            and int(pg_api.get("p1_n") or 0) == min(10, tot)
        p2_ok = True
        if tot > 10:
            p2_ok = int(pg_api.get("p2_n") or 0) == tot - 10
        # 界面：超过一页时必须出现分页条（"共 N 人 · 第 x / y 页"）；只有一页时不渲染
        pool_txt = c.eval("(document.getElementById('view').innerText||'')") or ""
        bar_ok = (("共 " in pool_txt and "页" in pool_txt) if tot > 10
                  else ("共 " not in pool_txt or "每页" not in pool_txt))
        # 导出不翻页：exportCsv 的源码里必须没有 page_size（它另发全量请求）
        export_all = c.eval("(()=>{const s=exportCsv.toString();"
                            "return !s.includes('page_size=');})()")
        print(f"  人才库分页：共 {tot} 人 / {exp_pages} 页；兼容口径(不带参数=全量) {compat_ok}；"
              f"第1页 {pg_api.get('p1_n')} 人(total={p1.get('total')}) {p1_ok}；"
              f"第2页断言 {p2_ok}；分页条渲染 {bar_ok}；导出不翻页 {export_all}")
        ok = ok and compat_ok and p1_ok and p2_ok and bar_ok and export_all
        c.shot(os.path.join(args.out, "18e-人才库-分页.png"))
        c.shot(os.path.join(args.out, "18c-人才库-批量归档与建议岗位.png"))

        # ⑦c-e 投递管道嵌入人才库（v1.7.2 下拉 → v1.7.4 看板 → v1.7.5 折叠定稿）：
        # 独立导航页退出；**默认折叠一行**只看各阶段人数（已入职/已结束终态不展示）；
        # 点「展开」出完整看板（在招 5 阶段列、人员全量、行内推进下拉、点人开档案）。
        c.goto(f"{args.url}/#pool")
        time.sleep(1.8)
        pool3 = c.eval("(document.getElementById('pipeCard')||{innerText:''}).innerText") or ""
        has_pipe_card = ("投递管道" in pool3) and ("pipeBtn" in
            (c.eval("(document.getElementById('view').innerHTML||'')") or ""))
        nav_pipe_gone = c.eval("!document.getElementById('tabs').innerHTML.includes(\"go('pipe')\")")
        collapsed_cols = c.eval("document.querySelectorAll('#view .pcol').length") or 0
        one_line_counts = ("在流程中" in pool3) and all(s in pool3 for s in
            ["新投递", "已联系", "初面", "复面", "待offer"])
        no_terminal = ("已入职" not in pool3) and ("已结束" not in pool3)
        c.eval("pipeToggle()")
        time.sleep(1.8)
        col_cnt = c.eval("document.querySelectorAll('#view .pcol').length") or 0
        all_stages = int(col_cnt) == 5          # 在招流程 5 列（已入职/已结束不占板）
        rows_clickable = "showDetail(" in (c.eval(
            "(document.getElementById('view').innerHTML||'')") or "")
        stage_sel_cnt = c.eval(
            "document.querySelectorAll('#view .pcol .it select').length") or 0
        # 点人 → 完整档案弹层真的打开（取管道里有 candidate_id 的第一条）
        pid = c.eval(
            "(async()=>{const r=await api('/api/pipeline');"
            "for (const s of ['新投递','已联系','初面','复面','待offer'])"
            "  for (const it of ((r.stages||{})[s]||{items:[]}).items)"
            "    if (it.candidate_id) return it.candidate_id;"
            "return null;})()", await_promise=True)
        modal_ok = False
        if pid:
            c.eval(f"showDetail({pid});")
            time.sleep(0.8)
            modal_ok = c.eval(
                "(document.getElementById('mTitle')||{textContent:''}).textContent.includes('完整档案')")
            c.eval("closeModal();")
        # 全部展示：展开后各列条目数与接口 items 数逐列一致（无截断）
        cap_ok = c.eval(
            "(async()=>{const r=await api('/api/pipeline');"
            "const dom=document.querySelectorAll('#view .pcol');"
            "const order=['新投递','已联系','初面','复面','待offer'];"
            "return order.every((s,i)=>{const v=(r.stages||{})[s]||{items:[]};"
            "const col=dom[i]; if(!col) return false;"
            "return col.querySelectorAll('.it').length===Math.max(1,(v.items||[]).length);});})()",
            await_promise=True)
        c.eval("pipeToggle()")                  # 收回默认态，不影响后续场景
        time.sleep(1.2)
        print(f"  投递管道折叠看板：卡片与按钮 {bool(has_pipe_card)}；独立导航页已撤 {bool(nav_pipe_gone)}；"
              f"默认折叠(0列) {int(collapsed_cols)==0}；一行人数 {bool(one_line_counts)}；"
              f"终态不上板 {bool(no_terminal)}；展开 5 列 {all_stages}；"
              f"条目与接口一致 {bool(cap_ok)}；推进下拉 {stage_sel_cnt} 个；点人弹档案 {bool(modal_ok)}")
        ok = ok and bool(has_pipe_card) and bool(nav_pipe_gone) and int(collapsed_cols)==0 \
            and bool(one_line_counts) and bool(no_terminal) and all_stages \
            and bool(cap_ok) and bool(rows_clickable) and bool(modal_ok)
        c.shot(os.path.join(args.out, "18f-人才库-投递管道折叠看板.png"))

        # ⑦c-f 初筛下拉（v1.7.3）：学历 ≥ 门槛 + 院校层次 985/211 + 卡片标签。
        # 数字对不上是最常见的坏法（下拉写 4、筛出来 3），所以每步都与接口 count 对账。
        has_edu_sel = c.eval("!!document.querySelector('select[onchange*=\"EDU_MIN\"]')")
        has_uni_sel = c.eval("!!document.querySelector('select[onchange*=\"UNIV\"]')")
        c.eval("EDU_MIN='本科';poolPageReset();refresh()")
        time.sleep(1.8)
        edu_cnt = c.eval(
            "(async()=>{const r=await api('/api/candidates?education=%E6%9C%AC%E7%A7%91');"
            "return r.count;})()", await_promise=True)
        edu_cards = c.eval("document.querySelectorAll('.pickChk').length") or 0
        edu_ok = bool(edu_cnt) and int(edu_cards) == min(10, int(edu_cnt))
        c.eval("UNIV='985';poolPageReset();refresh()")
        time.sleep(1.8)
        uni_cnt = c.eval(
            "(async()=>{const r=await api('/api/candidates?univ=985');"
            "return r.count;})()", await_promise=True)
        uni_cards = c.eval("document.querySelectorAll('.pickChk').length") or 0
        uni_ok = bool(uni_cnt) and int(uni_cards) == min(10, int(uni_cnt))
        c.eval("EDU_MIN='';UNIV='';poolPageReset();refresh()")
        time.sleep(1.8)
        pool4 = c.eval("(document.getElementById('view').innerHTML||'')") or ""
        any_uni = c.eval(
            "(async()=>{const r=await api('/api/candidates');"
            "return (r.items||[]).some(x=>!!x.uni_tier);})()", await_promise=True)
        tag_ok = (bool(any_uni) and (">985<" in pool4 or ">211<" in pool4)) or not any_uni
        print(f"  初筛下拉：学历/院校选择器 {bool(has_edu_sel)}/{bool(has_uni_sel)}；"
              f"学历≥本科 界面 {edu_cards} 人 = 接口 {edu_cnt} 人 {bool(edu_ok)}；"
              f"985 筛选 界面 {uni_cards} 人 = 接口 {uni_cnt} 人 {bool(uni_ok)}；"
              f"卡片 985/211 标签 {bool(tag_ok)}（库内 {'有' if any_uni else '无'}标签数据）")
        ok = ok and bool(has_edu_sel) and bool(has_uni_sel) and bool(edu_ok) \
            and bool(uni_ok) and bool(tag_ok)
        c.shot(os.path.join(args.out, "18g-人才库-学历与院校初筛.png"))

        # 归档页：勾选框 + 批量取消 + 满 30 天彻底删除的口径说明。
        # 归档页有没有人是"使用状态"而非验收输入，所以先保证有人再看细则：
        # 没人就临时归档一位（验完立刻取消），把"剩余天数"这条口径真正看到一遍。
        c.goto(f"{args.url}/#archive")
        time.sleep(1.5)
        arch_cnt = c.eval("document.querySelectorAll('.archChk').length") or 0
        _tmp_archived = None
        if not int(arch_cnt) and target:
            c.eval("window.__origConfirm2=window.confirm;window.confirm=()=>true;")
            c.eval(f"archiveCandidate({target},true);")
            for _ in range(16):
                time.sleep(0.4)
                arch_cnt = c.eval("document.querySelectorAll('.archChk').length") or 0
                if int(arch_cnt):
                    break
            _tmp_archived = target
            c.goto(f"{args.url}/#archive")
            time.sleep(1.5)
        arch2 = c.eval("(document.getElementById('view').innerHTML||'')") or ""
        has_unarch_batch = "批量取消归档" in arch2
        has_purge_fn = c.eval("typeof purgeOne==='function'")
        has_rule = "30 天" in arch2
        expired_btn = "已满 30 天，可彻底删除" in arch2
        left_chip = "天彻底删除" in arch2
        print(f"  归档页勾选框数：{arch_cnt}；批量取消入口：{bool(has_unarch_batch)}；"
              f"彻底删除入口就绪：{bool(has_purge_fn)}；30 天规则说明：{bool(has_rule)}；"
              f"剩余天数提示：{bool(left_chip)}；到期可直接删：{bool(expired_btn)}")
        ok = ok and bool(has_unarch_batch) and bool(has_purge_fn) and bool(has_rule)
        if int(arch_cnt) > 0:
            # 页面上有人时，必须能看到"还有 N 天"或"已满 30 天"的确切口径
            ok = ok and (bool(left_chip) or bool(expired_btn))
        c.shot(os.path.join(args.out, "18d-归档页-批量与彻底删除.png"))
        if _tmp_archived:
            c.eval(f"archiveCandidate({_tmp_archived},false);")
            for _ in range(16):
                time.sleep(0.4)
                _still = c.eval(
                    "(async()=>{const r=await api('/api/candidates');"
                    f"return (r.items||[]).some(x=>x.id==={_tmp_archived});}})()",
                    await_promise=True)
                if _still:
                    break
            print(f"  验收临时归档的人已还原（不改变库状态）：{bool(_still)}")
            c.eval("if(window.__origConfirm2)window.confirm=window.__origConfirm2;")

        # CSV 导出列口径（v1.5）：只验**实际写进表头的列**，不验函数源码里的注释——
        # 注释里出现"档位/推荐理由"是在说明为什么**不**导出它们，把它当违例是误判。
        csv_head = c.eval(
            "(()=>{const s=exportCsv.toString();"
            "const m=s.match(/const head = \\[([\\s\\S]*?)\\];/);"
            "return m?m[1]:'';})()") or ""
        csv_has_job = "对应岗位" in csv_head
        csv_no_score = not any(k in csv_head for k in ("档位", "推荐理由", "命中"))
        csv_ok = bool(csv_head) and csv_has_job and csv_no_score
        print(f"  CSV 导出表头：{csv_head.strip()[:70]}；含「对应岗位」：{csv_has_job}；"
              f"不含评分口径：{csv_no_score}")
        ok = ok and bool(csv_ok)

        # ⑦d 系统说明页：性别开关在页面上，且默认未勾选
        c.goto(f"{args.url}/#sys")
        time.sleep(1.6)
        sys_html = c.eval("(document.getElementById('view').innerHTML||'')") or ""
        gtoggle = c.eval("!!document.getElementById('gToggle')")
        gchecked = c.eval("(document.getElementById('gToggle')||{}).checked")
        print(f"  系统说明页含性别筛选开关：{bool(gtoggle)}；当前勾选状态：{gchecked}；"
              f"含合规依据说明：{'就业促进法' in sys_html}")
        ok = ok and bool(gtoggle) and ("就业促进法" in sys_html)
        c.shot(os.path.join(args.out, "19-系统说明-性别筛选开关.png"))

        # ⑦e 运行环境：模型口径必须"说全"，不能让降级看起来像正常。
        #    两处都实测踩过：① 向量模型不可达（Ollama 没起）时界面只写"384 维 · 已索引 N 人"，
        #    读起来像一切正常，而实际早已退回本地哈希向量；② 只有模型名、看不出走的是
        #    公网百炼还是内网 vLLM——这正是信创合规最需要一眼看到的东西。
        run_card = c.eval(
            "(()=>{const e=[...document.querySelectorAll('.k')].find(x=>x.innerText.trim()==='对话模型');"
            "if(!e) return ''; const b=e.closest('.card'); return b?b.innerText:'';})()") or ""
        meta = c.eval("(async()=>{const m=await api('/api/meta');"
                      "return {mreach:m.model.reachable, surl:m.model.base_url,"
                      "sreach:m.search.reachable, imodel:m.search.index_model,"
                      "sname:m.search.model};})()", await_promise=True) or {}
        has_base = bool(meta.get("surl")) and str(meta["surl"]) in run_card
        # 向量模型那行的可达状态必须与接口一致（不可达就该出现"不可达"字样）
        vec_honest = (("服务不可达" in run_card) if not meta.get("sreach")
                      else ("服务不可达" not in run_card))
        # 索引实际用的模型与配置不一致时，界面必须点出"实际使用 X"
        idx_honest = True
        if meta.get("imodel") and meta["imodel"] != meta.get("sname"):
            idx_honest = ("索引实际使用" in run_card and str(meta["imodel"]) in run_card)
        print(f"  运行环境是否说全：对话模型 base_url 可见 {has_base}；"
              f"向量模型可达状态如实 {vec_honest}；降级时点出「索引实际使用」{idx_honest}")
        ok = ok and has_base and vec_honest and idx_honest
        c.eval("(()=>{const e=[...document.querySelectorAll('.k')].find(x=>x.innerText.trim()==='对话模型');"
               "if(e) e.scrollIntoView({block:'center'});})()")
        time.sleep(0.6)
        c.shot(os.path.join(args.out, "22-系统说明-对话模型与向量模型.png"))

        # ⑧ 邮箱配置页：同样要有来源目录入口（此前只能手改配置文件）
        c.goto(f"{args.url}/#mailcfg")
        time.sleep(1.6)
        cfg_ok = c.eval("['cfgEmlDir','cfgFolderDir'].every(i=>!!document.getElementById(i))")
        cfg_dir_val = c.eval("(document.getElementById('cfgFolderDir')||{}).value") or ""
        cfg_max = c.eval("!!document.getElementById('cfgMaxMb')")
        cfg_preset = c.eval("!!document.getElementById('cfgPreset')")
        print(f"  邮箱配置页含「本地简历文件夹」输入框：{bool(cfg_ok)}（当前值 {cfg_dir_val}）；"
              f"含体积上限：{bool(cfg_max)}；含服务商预设：{bool(cfg_preset)}")
        ok = ok and bool(cfg_ok) and bool(cfg_dir_val.strip()) and bool(cfg_max) and bool(cfg_preset)
        c.shot(os.path.join(args.out, "17-邮箱配置-来源目录入口.png"))

        # ⑨ 智能助手页（v1.6 三项需求在界面上的落点）
        #   ① 历史会话：没点清空就不该丢——刷新页面后对话还在（此前刷新即空）
        #   ② 清空按钮真的存在且接的是 /api/agent/clear
        #   ③ 「清空 = 软清空」：保留期提示条 + 恢复对话入口（30 天内可恢复）
        #   ④ 档位解释/面试提纲上要能看出"这次用的是哪个岗位的尺子"与专业大类匹配
        c.goto(f"{args.url}/#chat")
        time.sleep(1.8)
        has_clear_btn = c.eval("typeof clearChat==='function' && "
                               "!!(document.querySelector(\"[onclick*='clearChat']\"))")
        has_hist_fn = c.eval("typeof loadChatHistory==='function'")
        hist_n = c.eval("(async()=>{const r=await api('/api/agent/history');"
                        "return (r.messages||[]).length;})()", await_promise=True)
        rendered = c.eval("(document.getElementById('chatLog')||{}).childElementCount")
        print(f"  智能助手页：清空按钮 {bool(has_clear_btn)}；历史拉取函数 {bool(has_hist_fn)}；"
              f"接口历史 {hist_n} 条；页面已渲染 {rendered} 个节点")
        ok = ok and bool(has_clear_btn) and bool(has_hist_fn)
        # 刷新页面后历史仍在（这就是"没点清空不该丢"的界面证据）
        c.goto(f"{args.url}/#chat")
        time.sleep(1.8)
        after_reload = c.eval("(document.getElementById('chatLog')||{}).innerText") or ""
        kept = len(after_reload.strip()) > 0
        empty_note = "只有点「清空」才会清空" in after_reload
        print(f"  刷新后对话仍在页面上：{kept}（{len(after_reload.strip())} 字）；"
              f"空态说明含清空口径：{empty_note}")
        # 有历史就必须真的渲染出来；没有历史时必须给出"为什么不空"的说明
        ok = ok and (kept or empty_note)
        c.shot(os.path.join(args.out, "20-智能助手-历史会话与清空.png"))

        # ④「清空 = 软清空」在界面上要有落点：恢复入口 + 保留期提示条。
        # 这里不改库（不真清空），而是把"已清空状态"直接喂给渲染函数，
        # 验证提示条会如实写出到期时间、且恢复按钮随之出现——界面不假装没有这条线。
        has_restore_btn = c.eval("typeof restoreChat==='function' && "
                                 "!!(document.getElementById('chatRestoreBtn'))")
        has_retention = c.eval("typeof renderChatRetention==='function' && "
                               "!!(document.getElementById('chatRetention'))")
        fake = c.eval(
            "(()=>{renderChatRetention({restorable:true,cleared_at:'2026-09-01 10:00:00',"
            "purge_after:'2026-10-01 10:00:00',days_left:30});"
            "const e=document.getElementById('chatRetention');"
            "const b=document.getElementById('chatRestoreBtn');"
            "return {shown:e.style.display!=='none', txt:e.innerText||'', "
            "btn:(b.style.display!=='none')};})()")
        print(f"  软清空界面落点：恢复按钮 {bool(has_restore_btn)}；保留期渲染函数 {bool(has_retention)}；"
              f"提示条 {fake.get('shown')}；文案含到期时间 "
              f"{'2026-10-01' in (fake.get('txt') or '')}")
        good = (bool(has_restore_btn) and bool(has_retention) and fake.get("shown")
                and fake.get("btn") and "2026-10-01" in (fake.get("txt") or "")
                and "30" in (fake.get("txt") or ""))
        print(f"  {'PASS' if good else 'FAIL'}  清空后能显示「将于 X 自动清理（还剩 N 天）」+ 恢复入口")
        ok = ok and good
        c.shot(os.path.join(args.out, "20b-智能助手-清空保留期与恢复入口.png"))
        # 把界面恢复成真实状态（避免留下伪装成"已清空"的提示条）
        c.eval("(async()=>{await loadChatHistory();})()", await_promise=True)

        # 档位解释弹层的"岗位口径 + 专业方向匹配"（挑一个有对应岗位的人）
        # ⚠️ 块的标题在 v1.7 由「专业大类匹配」改名为「专业方向匹配」，并新增了
        #    「置信度」与「通道」两处说明。断言必须跟着新标题走——否则界面是对的、
        #    探针却报失败（本轮就撞上：界面渲染正常，探针卡在旧标题上）。
        c.goto(f"{args.url}/#pool")
        time.sleep(1.8)
        cid = c.eval(
            "(async()=>{const r=await api('/api/candidates');"
            "for(const x of (r.items||[])){const e=await api('/api/candidates/'+x.id+'/explain',"
            "{method:'POST'});if(!e.error)return x.id;}return null;})()",
            await_promise=True)
        if cid:
            c.eval(f"(async()=>{{await explain({cid});}})()", await_promise=True)
            time.sleep(1.6)
            out = c.eval(f"(document.getElementById('out-{cid}')||{{}}).innerText") or ""
            has_jobline = "针对岗位" in out
            has_major = "专业方向匹配" in out
            has_conf = "置信度" in out
            # 通道说明：v1.7 起必须在结论旁点明"靠什么判的"（按技能大类 / 按专业维度 / 按文字比对）
            has_channel = any(k in out for k in ("按技能大类", "按专业维度", "按文字比对", "无可用依据"))
            # 部门已从系统中移除（2026-09 改版）：档位解释里不应再出现任何部门字样
            no_dept = "部门" not in out
            verdicts = [v for v in ("对口", "部分对口", "错配", "无法判定") if v in out]
            print(f"  档位解释 #{cid}：含「针对岗位」口径说明 {has_jobline}；"
                  f"含「专业方向匹配」块 {has_major}；含置信度 {has_conf}；"
                  f"含通道说明 {has_channel}；不含部门字样 {no_dept}；判定词 {verdicts}")
            # 口径说明、方向判定、置信度、通道、以及"已无部门"五者缺一不可
            ok = ok and has_jobline and has_major and has_conf and has_channel \
                and no_dept and bool(verdicts)
            c.shot(os.path.join(args.out, "21-档位解释-岗位口径与专业方向匹配.png"))
        else:
            print("  跳过档位解释：库中没有已归岗/有建议岗位的候选人")
            ok = False

    finally:
        c.close()

    print("界面交互态验收：" + ("全部通过" if ok else "存在未通过项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
