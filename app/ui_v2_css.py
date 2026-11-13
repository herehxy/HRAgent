"""v2 界面样式（从 ui_v2.py 拆出，避免单文件无限膨胀）。"""

_CSS = r"""
:root{
  /* v5 — Apple-inspired design tokens.
     Canvas = Apple 官网的浅灰 #f5f5f7，内容 = 纯白，文字 = #1d1d1f，
     功能色 = Apple Blue #0071e3；分隔线只保留一层极淡的 hairline。 */
  color-scheme:light;
  --font-ui:"SF Pro Display","SF Pro Text",-apple-system,BlinkMacSystemFont,
    "Segoe UI Variable Display","Segoe UI","PingFang SC","Hiragino Sans GB",
    "HarmonyOS Sans SC","Source Han Sans SC","Noto Sans CJK SC",
    "Microsoft YaHei UI","Microsoft YaHei",system-ui,sans-serif;
  --ease:cubic-bezier(.28,.11,.32,1);
  --ease-out:cubic-bezier(.16,1,.3,1);
  --ring:0 0 0 4px rgba(0,113,227,.16);
  --ground:#f5f5f7;
  --surface:#ffffff;
  --surface-2:#f2f2f5;
  --nav:#fbfbfd;
  --ink:#1d1d1f;
  --ink-2:#424245;
  --ink-3:#86868b;
  --line:#e8e8ed;
  --line-2:#d2d2d7;
  --accent:#0071e3;
  --accent-ink:#0066cc;
  --accent-soft:#eaf4ff;
  --accent-line:#cfe4fb;
  --accent-hi:#dcecfd;
  /* 导航 / 列表“选中”统一用这一套（聊天页也不跟着内容变色） */
  --sel:#eaf4ff; --sel-ink:#0066cc; --sel-line:#cfe4fb;
  --row-alt:#fafafc; --row-unit:#f0f0f3;
  /* 智能助手页统一回主蓝，不再单独使用暖色。 */
  --warm:#0071e3; --warm-soft:#eaf4ff; --warm-line:#cfe4fb;
  --plum:#5e5ce6; --plum-soft:#eeedff;
  --ok:#1d7f43;   --ok-soft:#e9f7ee;   --ok-line:#c9e9d5;   --ok-hi:#dcf2e5;
  --warn:#a05a00; --warn-soft:#fff6e8; --warn-line:#f0ddbb;
  --bad:#d70015;  --bad-soft:#fff0f2;  --bad-line:#f6ccd2;
  --tier-b:#0b6bb3; --tier-b-soft:#e8f3fb;
  --r:14px; --r-s:10px;
  --shadow:0 2px 6px rgba(0,0,0,.05),0 16px 40px rgba(0,0,0,.09);
  --fill-hover:rgba(0,0,0,.045);
  --fill-active:rgba(0,0,0,.07);
  --glass:rgba(255,255,255,.82);
  --scroll-thumb:rgba(0,0,0,.18);
}
/* Manual dark theme (set on <html> by the account menu) */
:root[data-theme="dark"]{
  color-scheme:dark;
  --ground:#000000; --surface:#1c1c1e; --surface-2:#2c2c2e; --nav:#161618;
  --ink:#f5f5f7; --ink-2:#d2d2d7; --ink-3:#98989d;
  --line:#38383a; --line-2:#48484a;
  --accent:#2997ff; --accent-ink:#7cbfff; --accent-soft:#0f2a44;
  --accent-line:#1f4668; --accent-hi:#163a5c;
  --sel:#0f2a44; --sel-ink:#7cbfff; --sel-line:#1f4668;
  --row-alt:#232325; --row-unit:#2a2a2d;
  --warm:#2997ff; --warm-soft:#0f2a44; --warm-line:#1f4668;
  --plum:#a99cff; --plum-soft:#2a2747;
  --ok:#5fd47f; --ok-soft:#122b1a; --ok-line:#265433; --ok-hi:#193b24;
  --warn:#f0b45c; --warn-soft:#2f2617; --warn-line:#594523;
  --bad:#ff7a70; --bad-soft:#3a1e21; --bad-line:#653036;
  --tier-b:#8ec5ea; --tier-b-soft:#1b2c38;
  --shadow:0 2px 8px rgba(0,0,0,.4),0 20px 50px rgba(0,0,0,.5);
  --ring:0 0 0 4px rgba(41,151,255,.22);
  --fill-hover:rgba(255,255,255,.07);
  --fill-active:rgba(255,255,255,.1);
  --glass:rgba(28,28,30,.82);
  --scroll-thumb:rgba(255,255,255,.24);
}
/* OS dark mode, unless the user explicitly chose light in the account menu. */
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --ground:#000000; --surface:#1c1c1e; --surface-2:#2c2c2e; --nav:#161618;
    --ink:#f5f5f7; --ink-2:#d2d2d7; --ink-3:#98989d;
    --line:#38383a; --line-2:#48484a;
    --accent:#2997ff; --accent-ink:#7cbfff; --accent-soft:#0f2a44;
    --accent-line:#1f4668; --accent-hi:#163a5c;
    --sel:#0f2a44; --sel-ink:#7cbfff; --sel-line:#1f4668;
    --row-alt:#232325; --row-unit:#2a2a2d;
    --warm:#2997ff; --warm-soft:#0f2a44; --warm-line:#1f4668;
    --plum:#a99cff; --plum-soft:#2a2747;
    --ok:#5fd47f; --ok-soft:#122b1a; --ok-line:#265433; --ok-hi:#193b24;
    --warn:#f0b45c; --warn-soft:#2f2617; --warn-line:#594523;
    --bad:#ff7a70; --bad-soft:#3a1e21; --bad-line:#653036;
    --tier-b:#8ec5ea; --tier-b-soft:#1b2c38;
    --shadow:0 2px 8px rgba(0,0,0,.4),0 20px 50px rgba(0,0,0,.5);
    --ring:0 0 0 4px rgba(41,151,255,.22);
    --fill-hover:rgba(255,255,255,.07);
    --fill-active:rgba(255,255,255,.1);
    --glass:rgba(28,28,30,.82);
    --scroll-thumb:rgba(255,255,255,.24);
  }
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font:14px/1.65 var(--font-ui);
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  text-rendering:optimizeLegibility;
  display:grid;grid-template-columns:248px 1fr;height:100vh;overflow:hidden;
}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer}
.num{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}

/* ---------------- 顶栏 ---------------- */
.top{
  flex:none;height:52px;display:flex;align-items:center;gap:10px;padding:0 16px;
  background:var(--surface);border-bottom:1px solid var(--line);
}
.brand{font-size:15px;font-weight:600;letter-spacing:.02em;margin-right:4px;flex:none;white-space:nowrap}
.brand i{
  display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--accent);
  margin-right:7px;vertical-align:1px;
}
.search{
  width:300px;height:32px;padding:0 10px;border:1px solid var(--line-2);border-radius:var(--r-s);
  background:var(--ground);outline:none;flex:none;
}
.search:focus{border-color:var(--accent);background:var(--surface);box-shadow:0 0 0 3px var(--accent-soft)}
.sel{
  height:32px;padding:0 8px;border:1px solid var(--line-2);border-radius:var(--r-s);
  background:var(--surface);color:var(--ink-2);flex:none;
}
/* 勾选式多选下拉：外观与普通 .sel 一致，点开是带复选框的浮层 */
.msel{position:relative;flex:none}
.msel > .msbtn{cursor:pointer;text-align:left;min-width:118px;max-width:210px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:inherit;
  font-size:13px;color:var(--ink-2)}
.msel > .msbtn.active{color:var(--accent-ink);border-color:var(--accent-line);
  background:var(--accent-soft)}
.msel .msPanel{display:none;position:absolute;top:36px;left:0;z-index:45;min-width:190px;
  max-height:280px;overflow-y:auto;background:var(--surface);border:1px solid var(--line-2);
  border-radius:var(--r);box-shadow:0 8px 22px rgba(24,28,32,.12);padding:4px}
.msel.open .msPanel{display:block}
.msItem{display:flex;align-items:center;gap:7px;padding:6px 8px;border-radius:4px;
  font-size:13px;color:var(--ink);cursor:pointer}
.msItem:hover{background:var(--accent-soft)}
.msItem input{width:14px;height:14px;accent-color:var(--accent);cursor:pointer}
.msFoot{border-top:1px solid var(--line);margin-top:4px;padding-top:5px;
  display:flex;justify-content:flex-end}
.sel:focus{outline:none;border-color:var(--accent)}
.count{margin-left:auto;color:var(--ink-3);font-size:13px;flex:none;white-space:nowrap}
.count b{color:var(--ink);font-size:15px;font-weight:600}

/* ---------------- 两栏 ---------------- */
/* ---------------- 第二行：动作 + 频道 ---------------- */
.top2{
  flex:none;min-height:44px;display:flex;align-items:center;gap:6px;padding:5px 12px;
  background:var(--surface);border-bottom:1px solid var(--line);
  /* 业务方 2026-10-27：**必须一行**，不许换行到第二排。
     之前是 flex-wrap:wrap，1150px 窗口下确实不会溢出，代价是最后几个按钮
     （"批量按模型重跑""分析待分析的人"）掉到第二行，整条工具栏变两行高。
     现在改成 nowrap + 紧凑尺寸（见下面的 .top2 .btn-lg），1920 窗口下
     整条约 1420px、可用 1724px，一行放得下还剩 300px。
     再窄的窗口才退化为横向滚动——仍然是一行，只是可以左右拖。 */
  flex-wrap:nowrap;overflow-x:auto;overflow-y:hidden;
  scrollbar-width:thin;
}
/* 这一条栏里的按钮比别处**小一档**：btn-lg 的 9px/18px 是给详情页那种
   稀疏布局定的，13 个按钮并排时每个省 10px 就是 130px。
   语义色（蓝=收进来 / 绿=发出去）和字重一概不动，只收紧内边距与字号。 */
.top2 .btn-lg{padding:7px 10px;font-size:13px;letter-spacing:-.01em}
/* 计数型按钮（归档勾选（0）/ 对比档案（0/10））的数字用等宽：
   数字变化时按钮宽度不跳，右边的按钮不会跟着左右滑。 */
.top2 .btn{font-variant-numeric:tabular-nums}
.top2::-webkit-scrollbar{height:6px}
.top2::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:3px}
/* 岗位管理页自己的那条栏。**不复用 .top2**：那一组"非人才库视图让位"规则
   （`body.v-org .top2{display:none!important}`）会把它一起隐藏掉。*/
.jobbar{
  flex:none;min-height:44px;display:flex;align-items:center;gap:8px;padding:6px 16px;
  background:var(--surface);border-bottom:1px solid var(--line);
}
.vdiv{width:1px;height:18px;background:var(--line-2);margin:0 2px;flex:none}
.allbox{display:inline-flex;align-items:center;gap:5px;font-size:13px;color:var(--ink-2);
  cursor:pointer;user-select:none;padding:0 4px;white-space:nowrap;flex:none}
.allbox input{width:14px;height:14px;accent-color:var(--accent);cursor:pointer}
.more{position:relative}
.morePanel{
  position:absolute;top:34px;left:0;z-index:30;display:none;flex-wrap:wrap;
  gap:8px;align-items:center;padding:10px 12px;background:var(--surface);
  border:1px solid var(--line-2);border-radius:var(--r);
  box-shadow:0 8px 22px rgba(24,28,32,.10);max-width:820px;
}
.morePanel.on{display:flex}
/* 「批量维护」是**动作菜单**，不是筛选面板：竖排、每项占满宽度、不换行。
   复用 .morePanel 的浮层外观（边框/阴影/圆角），只覆盖布局。 */
/* ⚠️ 这里必须 position:fixed，不能用 .morePanel 的 absolute。
   父级 `.top2` 为了"工具栏只占一行"设了 `overflow-x:auto;overflow-y:hidden`，
   而 CSS 规定：只要有一个轴不是 visible，另一个轴就会被算成 auto —— 于是浮层
   被工具栏那 44px 的高度整个裁掉，点开「批量维护」只看得到一条边（v3.1.16 修）。
   改成 fixed 后跳出裁切，位置由 `placeBatchPanel()` 按按钮的实时坐标算。 */
#batchPanel,#batchOpsPanel{flex-direction:column;flex-wrap:nowrap;gap:2px;padding:5px;min-width:170px;
  max-width:none;position:fixed;top:0;left:0}
#batchPanel .btn,#batchOpsPanel .btn{width:100%;text-align:left;border-color:transparent;background:transparent;
  padding:8px 10px;font-size:13.5px}
#batchPanel .btn:hover,#batchOpsPanel .btn:hover{background:var(--accent-soft);border-color:transparent;
  color:var(--accent-ink)}
/* 面板里所有筛选框**同宽同字号**（业务方 2026-10-21：四楞八样的宽度和字号看着乱）。
   `.sel` 原来由文字长度决定宽度，勾选式下拉又自带 min/max-width —— 统一成一把尺子。 */
.morePanel .sel,.morePanel .msel > .msbtn{
  width:172px;min-width:172px;max-width:172px;box-sizing:border-box;flex:none;
  font-size:14px;line-height:1.2}
.morePanel .lbl{font-size:13px;color:var(--ink-3)}
/* 投递时间：开始/结束两个日期框（业务方 2026-11-02 要求具体到天）。
   单独占一行，避免和 172px 的下拉挤在一起；宽度够显示完整 yyyy-mm-dd。 */
.morePanel .dateRange{flex:0 0 100%;display:flex;align-items:center;gap:8px;height:32px}
.morePanel .dateRange .lbl{white-space:nowrap}
.morePanel .dateRange input{
  width:150px;height:32px;padding:0 8px;border:1px solid var(--line-2);
  border-radius:var(--r-s);background:var(--surface);color:var(--ink-2);
  font:inherit;font-size:14px;box-sizing:border-box}
.morePanel .dateRange input:focus{outline:none;border-color:var(--accent)}
.morePanel .dateRange .dash{color:var(--ink-3);user-select:none}
/* 「清空筛选」单独占一行、靠右、用警示色（它是"推翻所有条件"的动作，
   混在一排下拉里既容易误点、也看不出来是按钮） */
.morePanel .clearRow{flex-basis:100%;display:flex;justify-content:flex-end;margin-top:4px;
  padding-top:8px;border-top:1px solid var(--line)}
.morePanel .clearRow .btn{color:var(--bad);border-color:var(--bad-line);
  background:var(--bad-soft);font-weight:600}
.morePanel .clearRow .btn:hover{background:var(--bad);color:#fff;border-color:var(--bad)}
.tabbar{flex:none;display:flex;align-items:center;min-height:38px;padding:4px 16px;
  background:var(--surface);border-bottom:1px solid var(--line)}
.tabs{display:flex;gap:2px;flex-wrap:wrap}
.tabs button{flex:none;white-space:nowrap}
.tabs button{
  border:1px solid transparent;background:transparent;color:var(--ink-2);
  padding:4px 10px;border-radius:14px;font-size:13px;
}
.tabs button:hover{background:var(--ground)}
.tabs button.on{
  background:var(--accent-soft);border-color:var(--accent-line);
  color:var(--accent-ink);font-weight:600;
}
/* ---------------- 左侧栏（模块切换） ----------------
   2026-11-02 重做：图标 + 单行账号。
   设计要求（业务方原话）："像 workbuddy 那样一行就可以"、"不要emoji、用图标"、
   "克制、克制、再克制" —— 所以这里只做三件事：
     · 每项 = 17px 线性图标 + 文字，行高 34px，间距统一 2px；
     · 分组标题 11px、字距 .08em，只做"分区提示"，不抢视线；
     · 账号压成一行（头像 22 + 名字 + 角色），点击出菜单。 */
.side{background:var(--nav);border-right:1px solid var(--line);
  padding:14px 8px 10px;overflow-y:auto;display:flex;flex-direction:column}
/* 品牌区 = 应用图标 + 产品名。图标用内联 data URI：
   这个页面是**单文件内联**的（JS/CSS 全在 HTML 里，见 render_page_v2），
   再挂一个 /logo.png 请求只会多一次往返，还会在服务刚起时闪一下空图。
   base64 是 icon_tray.png 缩到 96px 的版本，四角已透明，浅色侧栏上不露白边。 */
.sbrand{display:flex;align-items:center;gap:9px;font-size:14.5px;font-weight:600;
  padding:2px 10px 14px;letter-spacing:.01em;line-height:1.25}
.sbrand img{width:26px;height:26px;flex:none;display:block;
  border-radius:7px;box-shadow:0 1px 2px rgba(24,28,32,.14)}
.sbrand span{display:flex;flex-direction:column;min-width:0}
.sbrand b{font-weight:650;color:var(--ink);white-space:nowrap}
.sbrand small{font-size:11.5px;font-weight:400;color:var(--ink-3);
  white-space:nowrap;letter-spacing:.02em;margin-top:1px}
.sgroup{font-size:11px;color:var(--ink-3);padding:14px 10px 6px;
  letter-spacing:.08em;font-weight:500}
.side button{
  display:flex;align-items:center;gap:10px;width:100%;text-align:left;
  border:0;background:transparent;color:var(--ink-2);
  padding:0 10px;height:34px;border-radius:8px;font-size:14px;
  margin-bottom:2px;line-height:1;cursor:pointer;
}
.side button .ico{width:17px;height:17px;flex:none;stroke:currentColor;fill:none;
  stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;opacity:.66}
.side button:hover{background:var(--ground)}
.side button:hover .ico{opacity:.9}
.side button.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
.side button.on .ico{opacity:1;stroke-width:1.8}
.multi{display:inline-flex;flex-wrap:wrap;align-items:center;gap:5px;flex:1;min-width:230px}
.mchip{display:inline-flex;align-items:center;gap:4px;background:var(--accent-soft);
  border:1px solid var(--accent-line);color:var(--accent-ink);border-radius:4px;
  padding:2px 5px 2px 8px;font-size:13px}
.mchip b{cursor:pointer;font-weight:700;color:var(--ink-3);font-size:13px}
.tochip b{cursor:pointer;font-weight:700;color:var(--accent-ink);font-size:14px;
  margin-left:6px;opacity:.7}
.tochip b:hover{opacity:1;color:var(--bad)}
.mchip b:hover{color:var(--bad)}
#madd_ivunit{flex:1;min-width:150px;height:30px;padding:0 8px;border:1px solid var(--line-2);
  border-radius:var(--r-s);background:var(--surface);font:inherit;outline:none}
#madd_ivunit:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.chatwrap{display:flex;flex-direction:column;min-height:0;height:100%}
.chatlog{flex:1;min-height:0;overflow-y:auto;padding:18px 24px}
/* 消息气泡：**按内容宽度自适应**，不撑满也不贴边；整列限宽 880px。
   超宽屏上把气泡拉到 1100px，一句话占一整屏 —— 很难读。 */
.chatlog > *{max-width:880px}
.msg{display:inline-block;max-width:100%;margin:0 0 12px;padding:9px 13px;border-radius:8px;
  font-size:15px;line-height:1.7;vertical-align:top}
.msg.user{background:var(--accent-soft);color:var(--ink);border:1px solid var(--accent-line)}
.msg.assistant{background:var(--surface);border:1px solid var(--line);display:block}
.msg h3,.msg h4{margin:8px 0 6px;font-size:15px}
.msg blockquote{margin:6px 0;padding:4px 10px;border-left:2px solid var(--line-2);color:var(--ink-2)}
.msg code{background:var(--surface-2);padding:1px 5px;border-radius:4px;font-size:13px}
.msg ul{margin:6px 0;padding-left:20px}
.msg ol{margin:6px 0;padding-left:20px}
.msg pre{background:var(--surface-2);padding:9px 11px;border-radius:6px;overflow:auto;
  margin:8px 0;font-size:13px;line-height:1.55}
.msg pre code{background:none;padding:0}
/* 管道表格：模型做"全员对比"时几乎一定输出表格，没有它整块以原始 `|` 文本怼在页面上 */
.mdtable-wrap{margin:8px 0;overflow-x:auto}
.mdtable-bar{display:flex;gap:6px;justify-content:flex-end;margin-bottom:4px}
.mdt-btn{border:1px solid var(--line);background:var(--surface);color:var(--ink-3);
  border-radius:5px;padding:2px 8px;font-size:11.5px;cursor:pointer}
.mdt-btn:hover{color:var(--ink);border-color:var(--line-2);background:var(--surface-2)}
.mdtable{border-collapse:collapse;font-size:13px;min-width:100%}
.mdtable th,.mdtable td{border:1px solid var(--line);padding:5px 9px;text-align:left;
  vertical-align:top}
.mdtable th{background:var(--surface-2);font-weight:600;white-space:nowrap}
.mdtable tbody tr:nth-child(even){background:var(--surface-2)}
/* 助手输入框：**三行起步**，随内容长高，**上限 10 行**，再长就内部向上滚动；
   发送键放在框内右下角（不占外面的位置）。 */
.chatinput{flex:none;padding:12px 24px;border-top:1px solid var(--line);background:var(--surface)}
.ciwrap{position:relative}
.chatinput textarea{
  width:100%;min-height:calc(3 * 1.72em);max-height:calc(10 * 1.72em);
  padding:14px 92px 16px 11px;border:1px solid var(--line-2);
  border-radius:var(--r-s);font:inherit;font-size:15px;resize:none;outline:none;
  overflow-y:hidden;line-height:1.72;
  transition:padding-right .15s ease,border-color .15s ease;
}
.chatinput #detail textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
/* 发送/停止键：业务方 2026-10-21 要求"变大一点"。
   这两个键是高频操作目标，34px 高在 1500px 宽屏上偏小（旁边还有 392px 的右栏），
   放到 40px 并加 min-width，按下去命中率明显更稳。
   ⚠️ 高度变大后，输入框的 padding-bottom / padding-right 必须同步 ——
   否则键会压住最后一行字和右侧的引用标记。 */
#chatSend, #chatStop{
  position:absolute;right:8px;bottom:8px;transition:right .15s ease;
  min-height:40px;padding:0 18px;font-size:15px;font-weight:600;
}
#chatStop{position:absolute;right:8px;bottom:8px;background:var(--bad-soft);
  border-color:var(--bad-line);color:var(--bad);font-weight:600}
/* ⚠️ 「停止」出现时「发送」要**让开** —— 两个都是绝对定位在右下角，
   不让开就会叠在一起（业务方 2026-10-20 17:0x 截图反馈）。 */
#chatSend.busy{right:74px}
#chatStop:hover{background:var(--bad-soft);filter:brightness(.97)}
#chatSend.busy{opacity:.5;pointer-events:none}
.chatside{border-left:1px solid var(--line);background:var(--surface);padding:16px 18px;overflow-y:auto}
.metricpanel{display:flex;flex-direction:column;gap:6px;font-size:13px;color:var(--ink-2)}
.metric-row{display:flex;justify-content:space-between;gap:8px;padding:6px 9px;
  background:var(--surface-2);border-radius:6px}
.metric-k{color:var(--ink-3);flex:none;font-size:12.5px}
.metric-v{font-variant-numeric:tabular-nums;text-align:right;min-width:0;font-size:13px}
.ctxpanel{font-size:13px;color:var(--ink-2)}
.ctxmodel{font-size:14px;color:var(--ink-3);line-height:1.55;margin-bottom:6px;
  word-break:break-word}
.ctxmodel b{color:var(--ink);font-weight:700;font-size:15px}
.ctxhead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:5px}
.ctxhead b{font-size:15px;color:var(--ink)}
.ctxhead span{color:var(--ink-3);font-size:12.5px}
.ctxbar{display:flex;height:8px;border-radius:5px;overflow:hidden;
  background:var(--surface-2);margin:0 0 8px}
.ctxseg{height:100%;min-width:0}
.ctxrow{display:flex;align-items:center;gap:7px;padding:3px 0;font-size:12.5px}
.ctxdot{width:8px;height:8px;border-radius:2px;flex:none}
.ctxname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ctxnum{font-variant-numeric:tabular-nums;color:var(--ink-3);font-size:12.5px;flex:none}
.outpanel{display:flex;flex-direction:column;gap:4px;font-size:13px}
.outitem{display:flex;align-items:center;gap:7px;padding:6px 8px;
  border:1px solid var(--line);border-radius:6px;background:var(--surface-2);
  cursor:pointer;color:var(--ink-2);text-align:left;width:100%;font:inherit;
  font-size:12.5px}
.outitem:hover{border-color:var(--line-2);color:var(--ink)}
.outicon{width:17px;text-align:center;flex:none;color:var(--accent-ink)}
.outname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.outtag{font-size:11px;color:var(--ink-3);flex:none}
/* v4.0.17：右侧实时调用轨迹 */
.tracepanel{display:flex;flex-direction:column}
.tp-step{position:relative;padding:0 0 16px 22px}
.tp-step:before{content:"";position:absolute;left:5px;top:14px;bottom:0;width:1px;background:var(--line)}
.tp-step:last-child:before{display:none}
.tp-dot{position:absolute;left:0;top:3px;width:11px;height:11px;border-radius:50%;
  background:var(--accent);border:2px solid var(--surface);box-sizing:content-box}
.tp-dot.m{background:var(--plum)}
.tp-dot.t{background:var(--accent)}
.tp-dot.a{background:var(--ok)}
.tp-title{font-size:13px;font-weight:600;color:var(--ink)}
.tp-meta{font-size:11.5px;color:var(--ink-3);margin-top:2px;line-height:1.5;word-break:break-all}
.tp-badge{display:inline-block;margin:4px 6px 0 0;padding:1px 6px;border-radius:4px;
  font-size:10.5px;background:var(--warn-soft);color:var(--warn)}
.tp-badge.disabled{background:var(--bad-soft);color:var(--bad)}
.tp-running .tp-dot{animation:tpPulse 1s infinite}
.trace-empty{font-size:12.5px;color:var(--ink-3);line-height:1.7;padding:8px 0}
@keyframes tpPulse{0%,100%{opacity:1}50%{opacity:.35}}
.asks{display:flex;flex-direction:column;gap:6px;align-items:flex-start}
.askhead{display:flex;align-items:center;gap:8px}
.askhead .btn{margin-left:auto}
.ask{border:1px solid var(--line-2);background:var(--surface);border-radius:14px;
  padding:4px 11px;font-size:13px;color:var(--ink-2);text-align:left}
.ask:hover{border-color:var(--accent);color:var(--accent-ink);background:var(--accent-soft)}
.src-line{margin-top:6px;font-size:11.5px;color:var(--ink-3);line-height:1.6}
.msg-actions{margin-top:6px;display:flex;gap:6px}
.tracebox{margin-top:8px;font-size:13px}
.tracebox summary{cursor:pointer;color:var(--ink-3)}
.trace{margin-top:4px;padding:6px 9px;background:var(--surface-2);border-radius:5px;
  color:var(--ink-2);line-height:1.7}
/* 思考过程：生成中展开显示，答复落地后自动收成一个可点开的折叠块 */
.thinkbox{margin:0 0 10px;border:1px solid var(--line);border-radius:8px;
  background:var(--surface-2);overflow:hidden}
.thinkbox>summary{display:flex;align-items:center;gap:7px;padding:6px 10px;
  cursor:pointer;font-size:12.5px;color:var(--ink-3);user-select:none;list-style:none}
.thinkbox>summary::-webkit-details-marker{display:none}
.thinkbox>summary::before{content:'▸';font-size:10px;transition:transform .15s}
.thinkbox[open]>summary::before{transform:rotate(90deg)}
.thinkbox .think-meta{margin-left:auto;font-size:11.5px;color:var(--ink-3)}
.think-body{padding:0 11px 9px;font-size:12.5px;line-height:1.72;color:var(--ink-2);
  word-break:break-word;max-height:150px;overflow:auto}
.think-live{margin:0 0 8px;padding:8px 10px;border:1px dashed var(--accent-line);
  border-radius:8px;background:var(--accent-soft)}
.think-live-h{font-size:12px;color:var(--accent-ink);margin-bottom:4px}
/* 流式阶段先按纯文本追加（逐字可见），完成后再用 mdLite 渲染成排版 */
.think-live .think-body{white-space:pre-wrap}
.stream-answer{min-height:8px}
/* Mermaid 图表：饼图 / 折线图 / 柱状图，直接内联 SVG，不依赖外部库 */
.mchart{margin:10px 0;padding:8px;border:1px solid var(--line);border-radius:8px;
  background:var(--surface);overflow-x:auto}
.mchart-bar{display:flex;justify-content:flex-end;margin-bottom:2px}
.mchart svg{display:block;width:100%;height:auto;max-width:620px;margin:0 auto}
.dcard{margin-top:9px;padding:10px 12px;border:1px solid var(--accent-line);
  border-radius:var(--r-s);background:var(--accent-soft)}
.dcard.prop{border-color:var(--warn-line);background:var(--warn-soft)}
.dcard-t{font-weight:600;font-size:14px;margin-bottom:4px}
.dcard-b{font-size:13px;color:var(--ink-2);line-height:1.6}
.dcard-a{margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
/* 行内勾选框（来源文件逐文件选择用） */
.ck2{display:inline-flex;align-items:center;gap:5px;font-size:13px;color:var(--ink-2);cursor:pointer}
.ck2 input{width:14px;height:14px;accent-color:var(--accent);cursor:pointer}
/* 提案卡里的一条：标题 + 风险标 + 两个决定按钮，每条独立可点 */
.proprow{padding:7px 0;border-top:1px dashed var(--warn-line)}
.proprow:first-of-type{border-top:0;padding-top:2px}
.proprow-t{font-size:13px;line-height:1.6;color:var(--ink)}
/* 提案里的候选人姓名：点开右侧档案。做成明确的下划线链接，别让人猜可不可点 */
.plink{color:var(--accent-ink);text-decoration:none;border-bottom:1px dashed var(--accent-line);
  cursor:pointer}
.plink:hover{color:var(--accent);border-bottom-style:solid}
.proprow-a{margin-top:6px;display:flex;gap:6px;align-items:center}
/* 提案的前后值：让 HR 在卡片上核对完再点，不用跳档案盲签 */
.proprow-c{font-size:12.5px;color:var(--ink-2);margin-top:4px;line-height:1.55}
.proprow-c b{color:var(--ink-3);font-weight:500;margin:0 2px}
.proprow-st{font-size:12.5px;color:var(--ink-3)}
.proprow-st.approve{color:var(--ok);font-weight:600}
.proprow-st.reject{color:var(--bad);font-weight:600}
.proprow.done{opacity:.62}
/* 点下决定后、接口返回前：两个按钮一起禁用，旁边明确写出“选了哪一个”。 */
.propchoice{font-size:12.5px;font-weight:600;white-space:nowrap}
.propchoice.approve{color:var(--ok)}
.propchoice.reject{color:var(--bad)}
.btn.chosen{box-shadow:0 0 0 2px var(--accent-soft),0 0 0 3px var(--accent-line)}
/* 同类型提案超过 3 条时折叠成一组，避免一屏几十条。 */
.propgroup{margin:6px 0;border-top:1px dashed var(--warn-line);padding-top:6px}
.propgroup > summary{cursor:pointer;list-style:none;font-size:13px;font-weight:600;
  color:var(--ink);display:flex;align-items:center;gap:6px}
.propgroup > summary::-webkit-details-marker{display:none}
.propgroup > summary::before{content:'▸';color:var(--ink-3);font-size:12px}
.propgroup[open] > summary::before{content:'▾'}
.propgroup-hint{font-size:12px;color:var(--ink-3);font-weight:400;margin-left:auto}
.stg{font-size:12px;line-height:1;padding:2px 7px;border-radius:4px;margin-left:7px;
  font-weight:600;white-space:nowrap;display:inline-block;vertical-align:1px}
.ochip{font-size:13px;line-height:1;padding:3px 9px;border-radius:5px;margin-left:6px;
  font-weight:700;white-space:nowrap;display:inline-block;vertical-align:1px;
  border:1px solid transparent}
.ochip.g{color:var(--ok);background:var(--ok-soft);border:1px solid var(--ok-line)}
.ochip.r{color:var(--bad);background:var(--bad-soft);border-color:var(--bad-line);
  box-shadow:0 0 0 1px var(--bad-line)}
.jchip{display:inline-block;font-size:12px;padding:1px 7px;border-radius:4px;
  background:var(--surface-2);color:var(--ink-2);border:1px solid var(--line)}
.jchip.sug{background:var(--warn-soft);color:var(--warn);border-color:var(--warn-line)}
.jchip.none{color:var(--ink-3);background:transparent}
.ppager{display:flex;align-items:center;gap:6px;margin-top:4px;font-size:12px;color:var(--ink-3)}
.ppager .pg{border:1px solid var(--line-2);background:var(--surface);border-radius:4px;
  width:22px;height:20px;line-height:1;color:var(--ink-2);padding:0}
.ppager .pg:hover:not(:disabled){border-color:var(--accent);color:var(--accent-ink)}
.ppager .pg:disabled{opacity:.4;cursor:default}
/* 配置项：现值只读 + 「改」按钮；编辑区独占一行 */
.cfgsec h3{display:flex;align-items:center;gap:8px}
.cfgsec h3 .cfgacts{margin-left:auto;display:flex;gap:6px}
.cfgsec .ced{display:none}
.cfgsec.editing .cur{display:none}
.cfgsec.editing .ced{display:block}
/* 面试字典（2026-11-02 重做）：只读态只显示条目本身，编辑态才出现"添加"框和批量文本框。
   原来两行都在：上面一行"面试单位（还没有条目）[输入后回车添加]"，下面一行
   "面试单位 未设置" —— 同一个标题出现两次，用户不知道该用哪个。 */
.dict-bulk{display:none}
.cfgsec.editing .dict-bulk{display:block}
.dict-add{display:none}
.cfgsec.editing .dict-add{display:block}
.vsel{height:32px;padding:0 8px;border:1px solid var(--line-2);border-radius:var(--r-s);
  background:var(--surface);font:inherit;outline:none;cursor:pointer;min-width:110px}
.vsel:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.stagesel{height:32px;padding:0 8px;border:1px solid var(--line-2);border-radius:var(--r-s);
  background:var(--surface);font:inherit;outline:none;cursor:pointer}
.stagesel:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.cval{display:flex;align-items:center;gap:9px;flex:1;min-width:0}
.cval .ced{flex:1;min-width:0;height:34px;padding:0 10px;border:1px solid var(--line-2);
  border-radius:var(--r-s);background:var(--surface);font:inherit;outline:none}
.cval .ced[type=checkbox]{width:15px;height:15px;accent-color:var(--accent)}
.cval textarea.ced{height:auto;padding:8px 10px;line-height:1.6;resize:vertical}
.cval .ced:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.cval .cur{font-size:14px;color:var(--ink);word-break:break-all;white-space:pre-wrap;
  font-family:inherit;line-height:1.55}
.cval .cur.none{color:var(--ink-3)}
.btn.sm{height:26px;padding:0 10px;font-size:13px}
.cedit{background:var(--surface-2);border-radius:var(--r-s);padding:8px 10px;margin-bottom:4px}
.cinput{display:flex;align-items:flex-start;gap:8px;flex:1;min-width:0;flex-wrap:wrap}
.cinput input,.cinput select,.cinput textarea{
  flex:1;min-width:180px;height:34px;padding:0 10px;border:1px solid var(--line-2);
  border-radius:var(--r-s);background:var(--surface);font:inherit;outline:none}
.cinput textarea{height:auto;padding:8px 10px;line-height:1.6;resize:vertical}
.cinput input:focus,.cinput select:focus,.cinput textarea:focus{
  border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.crow{display:flex;align-items:flex-start;gap:10px;margin:6px 0}
.crow > label{width:86px;flex:none;font-size:13px;color:var(--ink-3);padding-top:7px}
.crow input,.crow select,.crow textarea{
  flex:1;min-width:0;height:34px;padding:0 10px;border:1px solid var(--line-2);
  border-radius:var(--r-s);background:var(--surface);font:inherit;outline:none}
.crow textarea{height:auto;padding:8px 10px;line-height:1.6;resize:vertical}
.crow input:focus,.crow select:focus,.crow textarea:focus{
  border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.ck{display:inline-flex;align-items:center;gap:6px;font-size:14px;padding-top:7px}
.ck input{width:15px;height:15px;accent-color:var(--accent)}
.archbar{flex:none;height:44px;align-items:center;gap:8px;padding:0 16px;
  background:var(--surface);border-bottom:1px solid var(--line);flex-wrap:wrap}
.propbox{display:none;max-height:320px;overflow-y:auto;padding:10px 16px;
  background:var(--surface);border-bottom:1px solid var(--line)}
.propbox.on{display:block}
.prow{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--line);
  font-size:14px}
.prow:last-child{border-bottom:0}
.prow.done{opacity:.45}
.ptext{flex:1;min-width:0}
.prisk{font-size:12px;padding:1px 6px;border-radius:4px;white-space:nowrap;
  background:var(--surface-2);color:var(--ink-3)}
.prisk.中{background:var(--warn-soft);color:var(--warn)}
.prisk.高{background:var(--bad-soft);color:var(--bad)}
.pacts{display:flex;gap:6px;flex:none}
#btnProp.on{background:var(--accent-soft);border-color:var(--accent-line);color:var(--accent-ink)}
.taskbar{display:flex;align-items:center;gap:12px;padding:8px 16px;background:var(--warn-soft);
  border-bottom:1px solid var(--warn-line);font-size:13px;color:var(--ink)}
.progtrack{height:10px;background:var(--surface-2);border-radius:6px;overflow:hidden;margin:12px 0 8px}
.progfill{height:100%;background:var(--accent);border-radius:6px;transition:width .3s ease}
.progmeta{font-size:13px;color:var(--ink-2);line-height:1.6}
/* 提示条：**浅绿底**（原版 ok-soft 口径）——原来用灰字太淡，看不出是"成功"还是"没跑" */
.msgline{display:inline-block;margin-left:8px;padding:3px 10px;border-radius:5px;
  background:var(--ok-soft);border:1px solid var(--ok-line);color:var(--ok);font-size:13px}
.msgline.bad{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}
.msgline.busy{background:var(--surface-2);border-color:var(--line);color:var(--ink-3)}
.pvbox{border:1px solid var(--line);border-radius:var(--r-s);background:var(--surface-2);
  padding:6px 10px;max-width:520px}
.pvhead{font-size:13px;color:var(--ink-3);margin-bottom:4px}
.pvrow{display:flex;align-items:baseline;gap:8px;padding:2px 0;font-size:13px;line-height:1.5}
.pvsub{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
.pvmeta{flex:none;max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:var(--ink-3);font-size:12px}
/* ---------------- 图表（纯 CSS/SVG，不引图表库） ---------------- */
.tier-donut{display:flex;align-items:center;gap:14px;flex:none}
.tier-donut svg .td-seg{cursor:pointer;transition:stroke-width .12s}
.tier-donut svg .td-seg:hover{stroke-width:18}
.tier-donut svg .td-total{cursor:pointer;transition:fill .12s}
.tier-donut svg .td-total:hover{fill:var(--accent-ink)}
.donutlegend{display:grid;gap:3px}
.lgitem{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--ink-2);
  cursor:pointer;padding:1px 4px;border-radius:4px}
.lgitem:hover{background:var(--surface-2)}
.lgitem i{width:8px;height:8px;border-radius:2px;display:inline-block;flex:none}
.lgitem span{flex:1;min-width:0}
.lgitem b{font-weight:600;color:var(--ink)}
.barchart{display:flex;flex-direction:column;gap:7px;max-width:640px}
.brow{display:flex;align-items:center;gap:10px;font-size:13px}
.blabel{width:126px;flex:none;text-align:right;color:var(--ink-2);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btrack{flex:1;min-width:0;height:14px;background:var(--surface-2);border-radius:3px;overflow:hidden}
.btrack i{display:block;height:100%;border-radius:3px;transition:width .25s ease}
.bnum{width:44px;flex:none;text-align:right;color:var(--ink);font-weight:600}
/* 环 + 图例一行，提示另起一行（挤在一行会与图例叠字） */
.anaring{display:block}
.anaringtip{color:var(--ink-3);font-size:13px;line-height:1.7;margin-top:10px}
.anagrp{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:6px 0}
/* 条件选项排成网格：每行放几个由宽度决定，不再由"这一行字有多长"决定 */
.anackgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(94px,1fr));
  gap:5px 10px;flex:1;min-width:0;align-items:center}
.anagname{width:78px;flex:none;color:var(--ink-3);font-size:13px}
.anack{display:inline-flex;align-items:center;gap:4px;font-size:13px;cursor:pointer;
  padding:2px 8px;border:1px solid var(--line-2);border-radius:12px;background:var(--surface)}
.anack input{width:13px;height:13px;accent-color:var(--accent);margin:0}
.anack:hover{border-color:var(--accent-line)}
.anares{margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.anacount{font-size:13px;color:var(--ink-2);margin-bottom:8px}
.anacount b{font-size:18px;color:var(--ink);margin-right:2px}
.anadim{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}
.dimbtn{border:1px solid var(--line-2);background:var(--surface);border-radius:12px;
  padding:2px 10px;font-size:13px;color:var(--ink-2)}
.dimbtn.on{background:var(--accent-soft);border-color:var(--accent-line);color:var(--accent-ink);font-weight:600}
/* 概览第一行：环 + 库存并排 */
.ovrow{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(400px,1.1fr);
  gap:22px;align-items:stretch}
.ovcol{min-width:0;background:var(--surface);border:1px solid var(--line);
  border-radius:8px;padding:14px 16px;display:flex;flex-direction:column}
.ovcol > h3{margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line);
  font-size:15px;font-weight:650;color:var(--ink);flex:none}
.ov-todos{flex:1 1 auto;min-height:0}
.ov-progress .anaring{display:flex;justify-content:center}
.ov-progress .anaringtip{text-align:center;max-width:420px;margin:10px auto 0}
/* v4.0.17：投递进度下方补“岗位投递人数”横向柱，填掉原来那块空白 */
.ov-jobs{margin-top:16px;padding-top:14px;border-top:1px solid var(--line)}
.ov-jobs h3{font-size:14px;margin:0 0 4px;color:var(--ink)}
.ov-jobs .ov-jobs-note{font-size:12px;color:var(--ink-3);margin:0 0 10px}
.ovjob-list{max-height:300px;overflow:auto;padding-right:4px}
.ovjob{margin-bottom:10px}
.ovjob-head{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;
  color:var(--ink-2);margin-bottom:5px}
.ovjob-head span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ovjob-head b{font-variant-numeric:tabular-nums;color:var(--ink);flex:none}
.ovjob-track{height:9px;background:var(--surface-2);border-radius:999px;overflow:hidden}
.ovjob-track i{display:block;height:100%;border-radius:999px;min-width:2px;
  background:linear-gradient(90deg,var(--accent),#6f9bff)}
.ovcol .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.ovgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 26px;
  align-items:start;grid-auto-flow:row dense}
.ovgrid > .sec{min-width:0}
.ovgrid > .ov-span2{grid-column:1/-1}
.ov-analysis{background:var(--surface);border:1px solid var(--line);border-radius:8px;
  padding:14px 16px}
.ana-workbench{display:grid;grid-template-columns:minmax(250px,360px) minmax(0,1fr);
  gap:20px;align-items:start;margin-top:12px}
.ana-controls{background:var(--surface-2);border:1px solid var(--line);border-radius:7px;
  padding:10px 12px}
.ana-controls .anagrp{margin:4px 0}
.ana-result{min-width:0}
.ana-result .barchart{max-width:none}
.anacondhead{display:flex;align-items:center;gap:12px;margin-bottom:8px}
.anack.on{background:var(--accent-soft);border-color:var(--accent-line);color:var(--accent-ink)}
.pgjump{height:26px;width:52px;text-align:center;border:1px solid var(--line-2);
  border-radius:4px;font:inherit;font-size:13px;outline:none}
.pgjump:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.contactline{display:flex;flex-wrap:wrap;gap:14px;align-items:center;font-size:14px}
.cval1{display:inline-flex;align-items:center;gap:6px}
.pgjump{height:26px;width:52px;text-align:center;border:1px solid var(--line-2);
  border-radius:4px;font:inherit;font-size:13px;outline:none}
.pgjump:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.contactline{display:flex;flex-wrap:wrap;gap:14px;align-items:center;font-size:14px}
.cval1{display:inline-flex;align-items:center;gap:6px}
.achip{font-size:12px;line-height:1;padding:2px 7px;border-radius:4px;
  margin-left:6px;white-space:nowrap;display:inline-block;vertical-align:1px}
.achip.exp{color:var(--bad);background:var(--bad-soft);border:1px solid var(--bad-line)}
.btn.danger{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}
.btn.danger:hover{background:var(--bad-soft);filter:brightness(.97)}
.jst{font-size:12px;line-height:1;padding:3px 7px;border-radius:4px;
  font-weight:600;white-space:nowrap;display:inline-block}
.jst.on{color:var(--ok);background:var(--ok-soft);border:1px solid var(--ok-line)}
.jst.off{color:var(--ink-3);background:var(--surface-2);border:1px solid var(--line)}
/* 左下角**账号区**（2026-10-30 加入，2026-11-02 压成一行）。
   业务方反馈："账号太大了，像 workbuddy 那样一行就可以，为什么这么高"。
   所以：头像 22px + 名字 + 角色**同一行**，整块高 36px，无边框、无卡片底色；
   点开是菜单：账号管理（有权限才显示）+ 退出登录。
   「系统配置」不在菜单里 —— 它已经是左侧导航的一项，重复放两处只会让人犹豫点哪个。 */
.sfoot{margin-top:auto;padding:8px 2px 2px;position:relative;
  border-top:1px solid var(--line);display:flex;align-items:center;gap:3px}
.acct{display:flex;align-items:center;gap:8px;flex:1;min-width:0;padding:0 8px;height:34px;
  border:0;background:transparent;border-radius:8px;cursor:pointer;
  text-align:left;font:inherit;color:var(--ink)}
/* `.side button` 的 width:100% 也会作用到账号行里的两个按钮；
   这里必须用 `.side .acct` / `.side .acctgear` 压过它，否则头像和齿轮会重叠。 */
.side .acct{width:auto;flex:1;min-width:0;height:42px}
.side .acctgear{width:36px;height:36px;flex:none;margin-left:auto;padding:0;
  border:0;background:transparent;border-radius:9px;
  display:grid;place-items:center;cursor:pointer;color:var(--ink-3)}
.side .acctgear:hover{background:var(--surface-2);color:var(--ink)}
.side .acctgear svg{width:21px;height:21px;fill:none;stroke:currentColor;stroke-width:1.55;
  stroke-linecap:round;stroke-linejoin:round}
.acctgear[hidden]{display:none!important}
.acct:hover{background:var(--ground)}
.acct:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.acct .av{width:22px;height:22px;border-radius:50%;flex:none;display:grid;place-items:center;
  background:var(--accent-soft);color:var(--accent-ink);font-weight:700;font-size:11px}
.acct .txt{flex:1;min-width:0;display:flex;align-items:baseline;gap:6px;overflow:hidden}
.acct .txt b{font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.acct .txt i{font-style:normal;font-size:11.5px;color:var(--ink-3);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.acct .chev{color:var(--ink-3);font-size:10px;flex:none;opacity:.55}
.acctMenu{position:absolute;left:2px;right:2px;bottom:calc(100% + 4px);z-index:60;
  background:var(--surface);border:1px solid var(--line);border-radius:10px;
  box-shadow:0 12px 30px rgba(16,24,40,.16);padding:4px;display:none}
.acctMenu.on{display:block}
.acctMenu button{display:block;width:100%;text-align:left;padding:0 10px;height:32px;
  border:0;background:transparent;border-radius:7px;cursor:pointer;font:inherit;
  color:var(--ink);font-size:13.5px;line-height:32px}
.acctMenu button:hover{background:var(--ground)}
/* ⚠️ 必须显式写：上面把 display 设成 block，会盖掉浏览器对 [hidden] 的默认隐藏，
   导致"没权限的账号管理"仍然显示出来（点了才 403）。 */
.acctMenu button[hidden]{display:none}
.acctMenu .ver{margin-top:2px;padding:7px 10px 3px;border-top:1px solid var(--line);
  font-size:11px;color:var(--ink-3);font-variant-numeric:tabular-nums}

.main{min-width:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* ---------------- 写邮件 ---------------- */
/* 非人才库视图：整条工具栏与主区都让位给该视图自己的内容。
   ⚠️ `.tabbar` 必须一起隐藏：它是人才库的档次筛选条（全部 / A / B / C /
   信息缺失 / 未复核），只对人才库列表有意义。漏掉它时，写邮件、岗位管理、
   归档、系统、对话五个页面顶部都会挂一条点了没反应的筛选条。 */
body.v-mail .top,body.v-mail .top2,body.v-mail .tabbar,body.v-mail .pipe,body.v-mail .wrap,
body.v-org  .top,body.v-org  .top2,body.v-org  .tabbar,body.v-org  .pipe,body.v-org  .wrap,
/* ⚠️ 类名必须和 setView 里 `document.body.className = 'v-' + v` 生成的**完全一致**：
   视图名是 archive → 类名是 v-archive（曾写成 v-arch，导致归档页里人才库主区还在）。 */
body.v-archive .top,body.v-archive .top2,body.v-archive .tabbar,body.v-archive .pipe,body.v-archive .wrap,
body.v-sys  .top,body.v-sys  .top2,body.v-sys  .tabbar,body.v-sys  .pipe,body.v-sys  .wrap,
body.v-users .top,body.v-users .top2,body.v-users .tabbar,body.v-users .pipe,body.v-users .wrap,
body.v-chat .top,body.v-chat .top2,body.v-chat .tabbar,body.v-chat .pipe,body.v-chat .wrap,
body.v-recruit .top,body.v-recruit .top2,body.v-recruit .tabbar,body.v-recruit .pipe,body.v-recruit .wrap,
body.v-schedule .top,body.v-schedule .top2,body.v-schedule .tabbar,body.v-schedule .pipe,body.v-schedule .wrap{display:none!important}
#mailView,#orgView,#archView,#sysView,#usersView,#chatView,#recruitView,#scheduleView{display:none;flex:1;min-height:0;overflow:hidden}
#usersView.on{display:block;overflow-y:auto}
/* ---------- 设置弹窗 ---------- */
.settingsmask{position:fixed;inset:0;z-index:120;background:rgba(0,0,0,.28);
  display:none;align-items:center;justify-content:center;padding:24px}
.settingsmask.on{display:flex}
.settingsdlg{width:min(1180px,96vw);height:min(840px,92vh);background:var(--ground);
  border:1px solid var(--line);border-radius:18px;box-shadow:0 24px 80px rgba(0,0,0,.22);
  display:grid;grid-template-columns:232px 1fr;overflow:hidden;position:relative}
.settingsnav{padding:20px 12px;border-right:1px solid var(--line);background:var(--nav)}
.settingsnav h3{font-size:16px;margin:0 10px 16px}
.settingsnav button{display:block;width:100%;text-align:left;border:0;background:transparent;
  color:var(--ink-2);font:inherit;font-size:14px;padding:10px 11px;border-radius:9px;cursor:pointer}
.settingsnav button:hover,.settingsnav button.on{background:var(--surface-2);color:var(--ink)}
.settingsbody{padding:28px 34px;overflow:auto;min-width:0}
.settingsbody .dhead{display:none}
.settingsclose{position:absolute;top:12px;right:14px;width:32px;height:32px;border:0;
  border-radius:50%;background:var(--surface-2);color:var(--ink-2);font-size:18px;cursor:pointer}
@media (max-width:800px){
  .settingsdlg{grid-template-columns:1fr;height:90vh}
  .settingsnav{display:flex;gap:4px;overflow:auto;border-right:0;border-bottom:1px solid var(--line);padding:10px}
  .settingsnav h3{display:none}
  .settingsnav button{white-space:nowrap;width:auto}
}
/* 写邮件：左侧准备材料，右侧专心编辑正文。去掉收件人预览后，编辑区获得主要宽度。 */
#mailView.on{display:grid;grid-template-columns:minmax(360px,.82fr) minmax(560px,1.18fr)}
#orgView.on{display:grid;grid-template-columns:330px 1fr}
/* 岗位页左列 = 列表(上) + 「新增岗位」(下)，右列 = 详情。
   ⚠️ 之前 jobBar 是 grid 的**第三个**子元素，会被自动放到第二行第一列 ——
   也就是**详情被挤进 330px 窄列**，文字全被压成竖排（业务方 2026-10-21 截图反馈）。*/
.orgleft{display:flex;flex-direction:column;min-height:0;min-width:0;
  border-right:1px solid var(--line)}
.orgleft .list{flex:1;min-height:0;overflow-y:auto}
.orgleft .jobbar{flex:none;border-bottom:none;border-top:1px solid var(--line)}
#archView.on{display:grid;grid-template-columns:392px 1fr;grid-template-rows:auto 1fr}
#sysView.on{display:block;overflow-y:auto;padding:18px 24px 40px}
#sysView > *{max-width:820px}
/* 招聘进度：整页滚动，表格自己横向滚动 + 表头 sticky。
   和系统配置页不同，这里内容很宽（单位×学历×签约人），不能限制 820px。 */
#recruitView.on{display:block;overflow-y:auto;padding:18px 24px 40px}
#scheduleView.on{display:flex;flex-direction:column;overflow:hidden;padding:20px 24px 18px}
#chatView.on{display:grid;grid-template-columns:250px minmax(0,1fr) 320px}
/* v4.0.17：Claude 式会话栏 */
.chatconv{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--line);
  background:var(--surface)}
.chatconv-head{padding:16px 14px 10px;display:flex;align-items:center;justify-content:space-between;gap:8px}
.chatconv-head h3{font-size:14px;margin:0}
.chatconv-list{flex:1;overflow-y:auto;padding:0 8px 14px;min-height:0}
.chatconv-item{position:relative;padding:10px 30px 10px 10px;border-radius:6px;cursor:pointer;margin-bottom:3px}
.chatconv-item:hover{background:var(--surface-2)}
.chatconv-item.on{background:var(--accent-soft)}
.cc-title{font-size:13.5px;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cc-meta{font-size:11.5px;color:var(--ink-3);margin-top:2px}
.cc-del{position:absolute;right:4px;top:50%;transform:translateY(-50%);border:0;background:transparent;
  color:var(--ink-3);font-size:16px;line-height:1;padding:3px 6px;border-radius:4px;opacity:0}
.cc-ren{position:absolute;right:26px;top:50%;transform:translateY(-50%);border:0;background:transparent;
  color:var(--ink-3);font-size:13px;line-height:1;padding:3px 5px;border-radius:4px;opacity:0}
.chatconv-item:hover .cc-del,.chatconv-item:hover .cc-ren{opacity:1}
.cc-del:hover{background:var(--bad-soft);color:var(--bad)}
.cc-ren:hover{background:var(--accent-soft);color:var(--accent-ink)}
.chatconv-empty{padding:22px 12px;color:var(--ink-3);font-size:12.5px;text-align:center;line-height:1.8}
/* 新会话欢迎区：输入第一句后由 pushMsg 移除 */
.chatwelcome{max-width:760px;margin:4vh auto 0;padding:0 2px 20px}
.cw-mark{width:46px;height:46px;border-radius:14px;background:var(--accent-soft);
  display:flex;align-items:center;justify-content:center;color:var(--accent-ink);font-weight:800;font-size:16px;
  letter-spacing:.5px;margin-bottom:18px}
.chatwelcome h2{font-size:27px;line-height:1.3;margin:0 0 8px;letter-spacing:-.4px}
.chatwelcome p{color:var(--ink-3);font-size:14px;margin:0 0 22px;max-width:620px}
.cw-asks{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.cw-card{text-align:left;background:var(--surface);border:1px solid var(--line);border-radius:8px;
  padding:12px 14px;cursor:pointer;transition:border-color .15s ease,background .15s ease}
.cw-card:hover{border-color:var(--accent-line);background:var(--accent-soft)}
.cw-card .cw-q{display:block;color:var(--ink);font-size:13.5px;line-height:1.5}
.cw-config{margin-top:12px;text-align:right}
@media(max-width:1280px){
  #chatView.on{grid-template-columns:220px minmax(0,1fr)}
  #chatView .chatside{display:none}
}
@media(max-width:900px){
  #chatView.on{grid-template-columns:1fr}
  #chatView .chatconv{display:none}
  .cw-asks{grid-template-columns:1fr}
}
#archBar{grid-column:1 / -1;border-bottom:1px solid var(--line)}
.mform{padding:18px 22px 40px;overflow-y:auto;background:var(--ground);
  border-right:1px solid var(--line)}
.mform > *{max-width:none}
.medit{padding:18px 24px 0;overflow-y:auto;background:var(--surface);
  display:flex;flex-direction:column;min-width:0}
.medit > .sec:first-child{margin-top:0}
.medit .richeditor{min-height:48vh}
.medit-actions{position:sticky;bottom:0;z-index:5;display:flex;align-items:center;
  gap:8px;flex-wrap:wrap;margin-top:12px;padding:12px 0 16px;
  border-top:1px solid var(--line);background:var(--surface)}
@media (max-width:1180px){
  #mailView.on{display:block;overflow-y:auto}
  .mform,.medit{overflow:visible;border-right:0}
  .medit{border-top:1px solid var(--line);padding-top:18px}
  .medit .richeditor{min-height:360px}
}
.mrow{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.mrow > label{width:64px;flex:none;font-size:13px;color:var(--ink-3)}
.mrow input,.mrow select{flex:1;min-width:0;height:34px;padding:0 10px;border:1px solid var(--line-2);
  border-radius:var(--r-s);background:var(--surface);outline:none;font-size:14px;box-sizing:border-box}
/* 变量区的控件在 `<div style="flex:1">` 包裹层里，**不是** .mrow 的直接子元素 ——
   上面那条 `flex:1` 管不到它，裸 input 只有浏览器默认的 ~177px，
   于是左栏拖宽后这些框还是原来那么窄（业务方 2026-10-21 反馈"格式太小"）。
   这里让包裹层内的控件填满，日期这类定长字段单独限宽，不跟着拉成一条长条。 */
.mrow > div > input,.mrow > div > select{width:100%;min-width:0;height:34px;
  padding:0 10px;border:1px solid var(--line-2);border-radius:var(--r-s);
  background:var(--surface);outline:none;font-size:14px;box-sizing:border-box}
.mrow > div > input[type="date"]{width:auto;max-width:220px}
.mrow > div > input:focus,.mrow > div > select:focus{
  border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.mrow input:focus,.mrow select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.mtool{display:flex;gap:4px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.richeditor{background:var(--surface);border:1px solid var(--line-2);border-radius:var(--r-s);
  padding:10px 12px;min-height:300px;outline:none;overflow-y:auto}
.richeditor:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.richeditor table{border-collapse:collapse;width:100%;margin:8px 0}
.richeditor td,.richeditor th{border:1px solid #bfbfbf;padding:6px 10px;vertical-align:middle}
.mtool .btn-primary{background:var(--accent-soft);border:1px solid var(--accent-line);
  color:var(--accent-ink);padding:2px 10px;border-radius:4px;font-size:13px}
.mtool button{height:26px;min-width:28px;padding:0 8px;border:1px solid var(--line-2);
  border-radius:4px;background:var(--surface);color:var(--ink-2);font-size:13px}
.mtool button:hover{border-color:var(--accent);color:var(--accent-ink)}
#mBody{width:100%;min-height:320px;padding:10px 12px;border:1px solid var(--line-2);
  border-radius:var(--r-s);background:var(--surface);font-size:14px;line-height:1.7;
  outline:none;overflow-y:auto}
#mBody:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
#mBody table{border-collapse:collapse;width:100%;margin:6px 0}
#mBody td,#mBody th{border:1px solid var(--line-2);padding:5px 8px;font-size:14px}
.ac{position:relative;flex:1;min-width:0}
/* 补全容器是 flex:1，但里面的 input 默认只有 ~177px —— 同样要显式填满 */
.ac > input{width:100%;min-width:0;box-sizing:border-box}
.acList{position:absolute;top:36px;left:0;right:0;z-index:30;max-height:260px;overflow-y:auto;
  background:var(--surface);border:1px solid var(--line-2);border-radius:var(--r);
  box-shadow:0 8px 22px rgba(24,28,32,.10);display:none}
.acList.on{display:block}
.acItem{padding:6px 10px;font-size:14px;cursor:pointer}
.acItem:hover{background:var(--accent-soft);color:var(--accent-ink)}
.acItem small{color:var(--ink-3);margin-left:8px}
.att{display:flex;align-items:center;gap:7px;font-size:14px;padding:3px 0}
.att input{width:14px;height:14px;accent-color:var(--accent)}
.pv{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;
  font-size:14px;line-height:1.75;min-height:200px}
.pv table{border-collapse:collapse;width:100%;margin:8px 0}
.pv td,.pv th{border:1px solid var(--line-2);padding:5px 8px;font-size:14px}
.tochip{display:inline-block;background:var(--accent-soft);border:1px solid var(--accent-line);color:var(--accent-ink);font-size:15px;font-weight:600;padding:5px 12px;border-radius:6px}
.vhint code{background:var(--ground);border:1px solid var(--line);border-radius:4px;padding:1px 6px;font-size:13px;color:var(--ink-2);margin-right:4px;display:inline-block;margin-bottom:4px}
.acItem.sel{background:var(--accent-soft);color:var(--accent-ink)}
.miss{color:var(--bad);font-size:13px;background:var(--bad-soft);border-radius:5px;padding:7px 10px;margin-bottom:10px}

.wrap{flex:1;min-height:0;display:grid;grid-template-columns:392px 1fr;overflow:hidden}

/* ---------------- 投递管道（第二行折叠） ---------------- */
.pipe{display:none;flex:none;height:auto;max-height:210px;gap:0;padding:8px 16px;background:var(--surface);
  border-bottom:1px solid var(--line);overflow-x:auto}
.pipe.on{display:flex;flex-wrap:wrap}
/* 管道面板头部：标题在左，「超期设置」在右上角。
   ⚠️ 管道是 flex 容器，头部必须 `flex:0 0 100%` **独占一整行** ——
   否则它会被当成一个普通列挤在左边，按钮跟着跑到最左边（实测过）。
   同时面板高度改成自适应（原来写死 134px，多一行头部就放不下了）。 */
.pipehead{flex:0 0 100%;display:flex;align-items:center;justify-content:space-between;
  gap:12px;margin-bottom:6px;padding-bottom:5px;border-bottom:1px solid var(--line)}
.pipeh-t{font-size:13px;font-weight:600;color:var(--ink-2)}
.pcol{flex:1 0 168px;min-width:168px;padding:0 10px 0 0;border-right:1px solid var(--line)}
.pcol:last-child{border-right:0;padding-right:0}
.pcol > h4{margin:0 0 6px;font-size:12px;font-weight:600;color:var(--ink-2);
  display:flex;align-items:center;gap:6px}
.pcol > h4 b{font-size:13px}
.pcol.zero > h4{color:var(--ink-3)}
.pchip{display:block;width:100%;text-align:left;border:0;background:transparent;
  padding:2px 6px;border-radius:4px;font-size:13px;color:var(--ink);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pchip:hover{background:var(--accent-soft);color:var(--accent-ink)}
.pmore{font-size:12px;color:var(--ink-3);padding:2px 6px}

/* 左：候选人列表 —— 一行一人，靠分隔线分隔，不做卡片 */
.listwrap{display:flex;flex-direction:column;min-height:0;background:var(--surface);
  border-right:1px solid var(--line)}
.list{flex:1;min-height:0;overflow-y:auto}
.pgwrap{display:inline-flex;align-items:center;gap:3px;white-space:nowrap}
.pager{flex:none;display:flex;align-items:center;gap:8px;padding:8px 12px;
  border-top:1px solid var(--line);font-size:13px;color:var(--ink-2);flex-wrap:wrap}
.row{
  display:grid;grid-template-columns:3px 18px 26px 1fr auto;gap:8px;align-items:center;
  padding:9px 12px 9px 0;border-bottom:1px solid var(--line);cursor:pointer;
}
/* 岗位行没有勾选框，列数另给一套 —— 否则状态标签会落进 1fr 那一列被拉满（实测宽 238px） */
.row.jrow{grid-template-columns:3px 26px 1fr auto}
.jrowacts{display:flex;align-items:center;gap:6px;flex:none}
.row:hover{background:var(--surface-2)}
.row{transition:background .12s ease}
.row.on{background:var(--accent-soft)}
.row.on .rowbar{background:var(--accent)}
.chk input{width:14px;height:14px;accent-color:var(--accent);cursor:pointer;display:block}
.rowbar{width:3px;height:100%;min-height:34px;background:transparent;border-radius:0 2px 2px 0;
  transition:background .14s ease}
.av{
  width:26px;height:26px;border-radius:50%;overflow:hidden;position:relative;flex:none;
  background:var(--ground);color:var(--ink-3);font-size:12px;font-weight:600;
  display:flex;align-items:center;justify-content:center;
}
.av img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.row .nm{font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .sub{font-size:13px;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tier{
  min-width:22px;height:20px;padding:0 6px;border-radius:4px;font-size:12px;font-weight:700;
  display:inline-flex;align-items:center;justify-content:center;
}
.tier.A{color:var(--ok);background:var(--ok-soft)}
.tier.B{color:var(--tier-b);background:var(--tier-b-soft)}
.tier.C{color:var(--ink-3);background:var(--surface-2)}
.empty{padding:28px 16px;color:var(--ink-3);font-size:13px;text-align:center}

/* 右：详情 */
/* 上内边距挪进 `.detail-toolbar`（见那里的说明）：否则 sticky 贴边后
   顶部会留一条露出滚动内容的缝。 */
.detail{overflow-y:auto;padding:0 24px 40px}
.detail.comparing{padding-top:0}
/* 右栏限宽：1600px 下内容只占左侧 60%，右边一大片空白会把视线拉散。
   限到 960px 后行宽落在舒适区（中文 ~45 字/行）。 */
.detail > *{max-width:1280px}
.detail > .detail-toolbar{max-width:none;width:auto;margin:0 -24px;padding:20px 24px 12px}
/* 操作条**常驻**（2026-10-28）：档案往下滚时，"保存更正 / 阶段 / 归档"要一直够得着。
   原先是 position:relative，跟着内容一起滚走 —— 看到页面下半部分想改阶段，
   得先滚回顶部。
   sticky 的容器是 `.detail`（它自己 overflow-y:auto），所以 top:0 贴的是详情栏顶边。 */
/* 常驻且**紧贴顶边**（2026-10-28）：sticky 的贴边基准是滚动容器的 padding box，
   而 `.detail` 有 `padding-top:20px` —— 于是操作条下方贴住了，上方那条 20px 的缝里
   会露出下面滚上来的档案内容（看起来像"漏出来"）。
   解法：把这段上内边距从容器挪进操作条自己（`padding-top:20px`），
   再用负左右外边距抵掉容器的左右 padding，让背景色铺满整栏宽度。 */
.detail-toolbar{
  position:sticky;top:0;z-index:6;display:flex;justify-content:flex-end;align-items:flex-start;
  margin:0;padding:14px 0 12px;background:var(--surface);
  border-bottom:1px solid var(--line);box-shadow:0 1px 0 rgba(31,35,41,.04);
}
.detail-toolbar .dact{flex:initial;width:100%;margin-left:0}
.profile-head{margin-top:22px;padding-top:0}
.profile-head .dname{line-height:1.4}
/* 姓名行：姓名在左、动作组贴最右（2026-10-30）。
   窄窗口下允许换行，动作组整体掉到第二行右对齐 —— 不允许把姓名挤成竖排。 */
.namerow{display:flex;align-items:flex-start;gap:14px;flex-wrap:wrap}
.namerow .dname{min-width:0}
.dhead-act{margin-left:auto;display:flex;gap:8px;align-items:center;flex:none;flex-wrap:wrap;
  padding-top:2px}
.profile-layout{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));
  gap:0 26px;align-items:start;grid-auto-flow:row dense}
.profile-layout > *{min-width:0}
.profile-layout > .profile-wide,
.profile-layout > .dupwarn,
.profile-layout > .quality,
.profile-layout > .srcbar,
.profile-layout > .edu-band{grid-column:1/-1}
/* 头部信息行字号在 `.dmeta` 里统一定义（17px，比姓名小一号）；
   这里原有一行 `font-size:15px` 因为选择器更具体会把它盖掉，已去掉。 */
.profile-layout{font-size:15px}
.profile-layout .sec > h3{font-size:14px}
.profile-layout .hint{font-size:14px}
.profile-layout .ev{font-size:15px;line-height:1.7}
.profile-layout .sum{font-size:16px}
.profile-layout .edu-line{font-size:15px}
.profile-layout .edu-degree,.profile-layout .edu-major{font-size:14px}
.profile-layout .doc{font-size:15px}
.profile-layout .fld > .fl,.profile-layout .lbl2{font-size:14px}
.profile-layout .skill span{font-size:14px}
.profile-layout .chipl{font-size:14px}
.profile-layout .edv{font-size:15px}
.profile-layout input,.profile-layout select,.profile-layout textarea{font-size:15px}
.profile-layout .rawbox pre{font-size:13.5px}
.profile-layout .auditbox{font-size:14px}
.dhead{display:flex;align-items:flex-start;gap:14px}
/* 头部两行：上=姓名+操作按钮，下=基本信息。信息与姓名左对齐、头像顶对齐 */
.dmain{flex:1;min-width:0}
.dtop{display:flex;align-items:center;gap:16px}
.dtop .dname{flex:none;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 证件照是"认人"的第一眼信息，52px 只够看出有人脸（2026-10-28 放大到 96px）。
   用圆角方框而不是圆形：证件照多是竖版白底证件照，圆形会把头顶和衣领裁掉；
   圆角方框能保留整张脸和肩线，也更像人事档案里的照片位。 */
.av-lg{
  width:96px;height:96px;border-radius:10px;overflow:hidden;position:relative;flex:none;
  background:var(--surface);border:1px solid var(--line);color:var(--ink-3);
  font-size:30px;font-weight:600;display:flex;align-items:center;justify-content:center;
}
.av-lg img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.dname{font-size:24px;font-weight:650;letter-spacing:-.01em;line-height:1.25}
/* 姓名可点改：hover 才显出底色，平时保持大标题的干净 */
.dname.edu-click{border-bottom-color:transparent}
.dname.edu-click:hover{background:var(--accent-soft);color:var(--accent-ink)}
/* ---------- 头部信息区（2026-10-28 重做） ----------
   问题：性别/岗位/状态/电话/邮箱**五种不同性质的信息**全做成同样大小的白底方框，
   还有五六个边框，远看是一排一样的东西 —— 没有层级，也不整齐。
   现在按"这一行是什么"分三段，各自一种形状：
     ① 事实（性别 · 岗位 · 状态）：**无框纯文字**，中间用小圆点分隔，像一句陈述；
     ② 竖线：把"人是谁"和"怎么联系"分开；
     ③ 联系方式：浅灰底的胶囊（可点改），比纯文字重、比阶段/结论轻。 */
/* 字号：比姓名（24px）小一号（17px）。这一行是"这个人是谁"的全部关键信息，
   13.5px 在 24px 的姓名旁边显得像脚注（业务方 2026-10-28 反馈）。 */
.dmeta{margin-top:6px;display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;
  font-size:17px;color:var(--ink-2)}
.dfacts{display:inline-flex;align-items:center;min-width:0}
/* ⚠️ 必须带 `#detail` 前缀：下面"详情页整体放大一号"那段有 `#detail .chipl{font-size:14px}`，
   ID 选择器会把这里的字号盖掉（2026-10-28 踩过：改了等于没改）。 */
#detail .dmeta .chipl{background:transparent;border-color:transparent;color:var(--ink-3);
  font-weight:500;font-size:16px;padding:0;border-radius:0}
.dmeta .chipl.jchip{color:var(--ink-2);font-weight:600}
.dmeta .chipl.jchip.none{color:var(--warn)}
.dmeta .chipl.jchip:hover{background:transparent;border-color:transparent;
  color:var(--accent-ink);text-decoration:underline}
.fdsep{width:4px;height:4px;border-radius:50%;background:var(--line-2);
  margin:0 10px;flex:none}
.vsep{width:1px;height:16px;background:var(--line);margin:0 4px;flex:none}
.dcontact{display:inline-flex;align-items:center;gap:8px;min-width:0}
.dmeta .edv{background:var(--surface-2);border:1px solid transparent;color:var(--ink);
  font-weight:500;font-size:16px;padding:3px 11px;border-radius:6px}
.dmeta .edv:hover{background:var(--surface);border-color:var(--accent-line);
  color:var(--accent-ink)}
.dmeta .edv.empty{color:var(--ink-3);font-weight:400}
/* 第二行：阶段 / 结论 / 录用函 / 数据质量 —— 这些才是"判断类"信息，
   给实底（阶段深灰、结论绿/红、录用函浅绿），与上面那行的事实区拉开层级。 */
.dmeta.dmeta2{margin-top:10px;gap:8px}
/* 同样要带 `#detail`：上面 `#detail .dmeta .chipl` 会连第二行一起放大 */
#detail .dmeta.dmeta2 .chipl{font-size:14px;font-weight:600;padding:3px 11px;border-radius:6px;
  background:var(--surface-2);border:1px solid var(--line);color:var(--ink-2)}
.dmeta.dmeta2 .stagechip{background:#3f4753;border-color:#3f4753;color:#fff}
.dmeta.dmeta2 .offerchip{background:var(--ok-soft);border-color:var(--ok-line);
  color:var(--ok)}
.dmeta.dmeta2 .ochip{font-size:14px;padding:4px 11px;margin-left:0}
/* 岗位标签做成可点：看起来是标签，点下去是"改岗位" */
.jchip{cursor:pointer;font-family:inherit;line-height:1.5}
.jchip:hover{background:var(--accent-soft);border-color:var(--accent-line);
  color:var(--accent-ink)}
/* 疑似重复提示：只由姓名完全一致触发，暖色底提醒但不示警（同名真的可能是两个人）。
   右侧那个按钮是"确认过了，别再提示"，所以整条做成 flex，按钮贴着右边。 */
.dupwarn{background:var(--warn-soft);border:1px solid var(--warn-line);border-radius:var(--r);
  padding:7px 11px;margin:10px 0 0;font-size:13px;color:var(--ink);
  display:flex;align-items:center;gap:12px}
.dupwarn-t{flex:1;min-width:0}
.dupwarn .btn{flex:none;background:var(--surface);border-color:var(--warn-line);
  color:var(--ink-2)}
.dupwarn .btn:hover{background:var(--warn);color:#fff;border-color:var(--warn)}
.dupi{display:inline-block;margin-left:10px;color:var(--ink-2)}
/* 敏感字段剔除明细：绿底＝隐私处理已生效；none 用中性底色，明说"没留痕"≠"没敏感信息" */
.sensline{background:var(--ok-soft);border:1px solid var(--ok-line);border-radius:var(--r);
  padding:7px 11px;margin-bottom:9px;font-size:13px;color:var(--ink)}
.sensline.none{background:var(--surface-2);border-color:var(--line);color:var(--ink-2)}
/* （原有一条重复的 .dmeta 规则在这里，字号 14px 把上面 17px 覆盖掉了 —— 已合并进上方） */
.chipl{
  padding:2px 8px;border-radius:4px;font-size:13px;font-weight:700;
  color:var(--accent-ink);background:var(--accent-soft);border:1px solid var(--accent-line);
}
/* 软科排名是业务方明确要看重的信息 —— 不当灰注释，给强调色与字重 */
.rk{color:var(--accent-ink);font-size:13px;font-weight:600;background:var(--accent-soft);
  border-radius:4px;padding:1px 6px}
.rk.none{color:var(--ink-3);background:transparent;font-weight:400}
.chipl.gray{color:var(--ink-3);background:var(--ground);border-color:var(--line)}
/* 操作按钮：独占第一行右侧；宽度不够时**只在这一块里换行**，不会挤到下面的信息行 */
.dact{margin-left:auto;display:flex;gap:8px;align-items:center;
  flex-wrap:wrap;justify-content:flex-end;flex:1;min-width:0}
/* 多档案固定标签：同一时间只展示一个完整档案，像浏览器标签页一样切换。 */
/* 对比档案的固定条：**紧贴上方**（业务方 2026-10-21：不要留缝），
   与下方正文之间靠 padding-bottom 留出呼吸。
   ⚠️ 原来的 `margin-top:-20px` 是"当它是详情区第一个子元素时"用来抵消 padding 的；
   现在它前面还有工具条与档案头部，负边距会让它往上压、出现错位 —— 改成 0。 */
.pinned-tabs{position:sticky;top:0;z-index:22;display:flex;align-items:stretch;gap:8px;
  min-height:38px;margin:0 -24px;padding:0 24px;background:var(--surface);
  border-bottom:1px solid var(--line-2)}
.pinned-tab-list{display:flex;align-items:stretch;gap:4px;min-width:0;flex:1;
  overflow-x:auto;padding-bottom:0}
.pinned-tab{display:flex;align-items:center;gap:7px;flex:none;max-width:220px;
  padding:8px 8px 8px 11px;margin-bottom:-1px;border:1px solid var(--line);
  border-bottom:0;border-radius:7px 7px 0 0;background:var(--surface-2);
  color:var(--ink-2);cursor:pointer;font-size:13px;white-space:nowrap}
.pinned-tab:hover{background:var(--accent-soft)}
.pinned-tab.on{background:var(--surface);color:var(--accent-ink);font-weight:600;
  border-color:var(--line-2);position:relative;z-index:1}
.pinned-tab .pt-name{max-width:150px;overflow:hidden;text-overflow:ellipsis}
.pinned-tab .pt-x{border:0;background:transparent;color:var(--ink-3);font-size:16px;
  line-height:1;padding:1px 3px;border-radius:3px;flex:none}
.pinned-tab .pt-x:hover{background:var(--bad-soft);color:var(--bad)}
.pinned-tab-actions{flex:none;display:flex;align-items:center;gap:8px;
  align-self:stretch;background:var(--surface)}
.pinned-profile{min-width:0;padding-top:18px}

/* 档位：三个按钮，选中即改（不弹确认——上下文里一眼能看出改了什么） */
.seg{display:inline-flex;border:1px solid var(--line-2);border-radius:var(--r-s);overflow:hidden}
.seg button{
  border:0;background:var(--surface);color:var(--ink-2);padding:5px 13px;font-size:13px;
  border-right:1px solid var(--line-2);
}
.seg button:last-child{border-right:0}
.seg button:hover{background:var(--ground)}
.seg button.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:700;
  box-shadow:inset 0 0 0 1.5px var(--accent)}
.btn{
  border:1px solid var(--line-2);background:var(--surface);border-radius:var(--r-s);
  padding:6px 13px;font-size:13px;color:var(--ink);
  /* ⚠️ 必须 flex:none + nowrap：否则工具条里按钮会被压成竖排（一个字一行） */
  flex:none;white-space:nowrap;line-height:1.5;
}
.btn:hover{border-color:var(--accent);color:var(--accent-ink)}
.btn.primary{background:var(--accent-soft);border-color:var(--accent-line);
  color:var(--accent-ink);font-weight:700}
.btn.primary:hover{background:var(--accent-hi);border-color:var(--accent);color:var(--accent-ink)}
/* 「已复核」态（2026-11-02）：按下之后按钮本身要能一眼看出来，
   天蓝底 + 深蓝字 + 前面一个对勾；悬停提示"点击撤销复核"。
   原来按下后文字变成"撤销复核"，用户反而看不出"这个人复核过了没有"。 */
.btn.reviewed{background:#e6f4fd;border-color:#9fd4f3;color:#0f6fa8;font-weight:600}
.btn.reviewed:hover{background:#d6ecfa;border-color:#7cc4ea;color:#0b5c8c}
.btn:disabled{opacity:.45;cursor:not-allowed}

/* 分节：一条细线 + 小标题，不用卡片 */
.sec{margin-top:22px}
.sec > h3{
  margin:0 0 11px;font-size:13px;font-weight:600;letter-spacing:.06em;
  color:var(--ink-3);text-transform:none;border-bottom:1px solid var(--line);padding-bottom:7px;
}
/* 档位是这张档案的**判断结论** —— 全页唯一"用力"的地方。
   其它区块标题统一是 13px 灰字 + 分割线（背景/说明性质），
   这里提到正文色、加大一档、去掉分割线，并让右侧档位按钮更实。
   原则：把强调只用在一处，四周保持安静（否则每块都在喊，等于没人在喊）。*/
/* 区块标题右侧带操作（如「复制」）：标题变 flex，按钮靠右、垂直居中 */
.sec > h3.ha{display:flex;align-items:center;gap:8px}
.sec > h3.ha .h3act{margin-left:auto;display:inline-flex;gap:6px}
.sec.tier-sec{margin-top:18px}
.sec.tier-sec > h3{
  color:var(--ink);font-size:15px;font-weight:650;letter-spacing:0;
  border-bottom:0;padding-bottom:0;margin-bottom:8px;
}
.tier-sec .seg button{font-size:14px;padding:6px 16px}
.kv{display:grid;grid-template-columns:96px 1fr;gap:8px 14px;font-size:14px}
.kv dt{color:var(--ink-3)}
.kv dd{margin:0;color:var(--ink)}
.skill{display:flex;gap:8px;flex-wrap:wrap}
.skill span{
  padding:3px 10px;border-radius:4px;font-size:13px;border:1px solid transparent;
}
/* ---------- 完整档案：正文整体放大一号（业务方 2026-10-21：字太小，看久了费眼） ----------
   只动档案详情区，不影响左侧列表与其它页面。
   放大一档（+1px）而不是两档：再大就撑破现有的行高与标签间距了。 */
#detail .ev{font-size:15px;line-height:1.8}
#detail .ev b{font-size:15px}
#detail .doc{font-size:15px}
#detail .doc-n{font-size:15px}
#detail .skill span{font-size:14px;padding:4px 11px}
#detail .kv{font-size:15px}
#detail .sec > h3{font-size:14px}
#detail .sec .hint{font-size:13.5px}
#detail .chipl{font-size:14px}
#detail .rk{font-size:14px}
#detail .auditbox{font-size:14px}
#detail .auditbox th{font-size:13px}
#detail .auditbox td{font-size:14px;padding:6px 11px}

/* ---------- 详情页：投递来源一行 + 面试与录用一行（2026-10-28） ----------
   两块改动都出于同一个理由：**右栏是"判断一个人"的地方，视线要能顺着一条线走完**
   「是谁 → 材料从哪来 → 怎么判的 → 走到哪一步」，而不是在页面上下反复找。 */
/* 底色用**浅鼠尾草绿**，不是状态绿（--ok 那套表示"命中/通过"）：
   这一行的语义是"材料来源"，需要一个和正文灰、和状态色都不撞的第三种色，
   同时又要足够浅，不抢档位那一处的强调。
   对比度：#2f5a33 on #eef4ec ≈ 7.5:1；按钮文字同色，hover 反白。 */
.srcbar{display:flex;align-items:center;gap:12px;margin:14px 0 0;padding:9px 12px;
  background:#eef4ec;border:1px solid #dbe7d8;border-radius:var(--r);
  font-size:13.5px;min-width:0;flex-wrap:wrap}
.srcbar > .srck{flex:none;color:#4b7a51;font-weight:600}
.srcbar > .srcmail{display:flex;align-items:baseline;gap:8px;min-width:0;flex:1 1 auto;
  overflow:hidden;white-space:nowrap}
/* 邮件主题是这一行里信息量最大的，必须让它先拿到宽度：
   flex:1 1 auto + min-width 保证它不会被发件人/文件名挤成几个字。 */
.srcbar .srcsub{color:#2f5a33;font-weight:600;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:1 1 auto;min-width:180px}
.srcbar .srcmeta{color:#5c7a60;flex:none;font-size:13px;max-width:18ch;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 同一封邮件可能带 2 份以上简历（2026-11-05 业务方反馈“显示共两份、在线打开只有一个”）。
   一份时和原来一样贴在右侧；多份时整块换到第二行，逐份列出，每份都有自己的打开/下载。 */
.srcbar > .srcfiles{flex:none;display:flex;flex-direction:column;gap:5px;min-width:0;
  border-left:1px solid #cfe0cc;padding-left:12px}
.srcbar > .srcfiles.many{flex:1 1 100%;border-left:0;padding-left:0;margin-top:1px;
  padding-top:7px;border-top:1px solid #d3e2d0}
.srcbar > .srcfiles .srccount{max-width:none;color:#4b7a51;font-weight:600}
.srcbar .srcfile{display:flex;align-items:center;gap:6px;min-width:0}
.srcbar .srcfile .fn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:#3c6b41;max-width:20ch}
.srcbar > .srcfiles.many .srcfile{width:100%}
.srcbar > .srcfiles.many .srcfile .fn{max-width:40ch;flex:0 1 auto}
.srcbar > .srcfiles.many .srcfile .btn:first-of-type{margin-left:auto}
/* 按钮跟着这一行的绿走：默认白底绿字，hover 反白。
   不用 btn.primary（那是蓝色的"主操作"语义），这里只是同一族里的次级动作。 */
.srcbar .btn{height:26px;padding:0 9px;font-size:13px;background:#fff;
  border:1px solid #bcd4b9;color:#2f5a33;border-radius:var(--r-s)}
.srcbar .btn:hover{background:#2f5a33;border-color:#2f5a33;color:#fff}

.ivsec{margin-top:20px}
.ivrow{display:flex;align-items:flex-start;gap:14px;flex-wrap:wrap}
.ivrow + .ivrow{margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.ivrow .fld{display:inline-flex;flex-direction:column;align-items:flex-start;gap:4px}
.ivrow .fl{font-size:13.5px;color:var(--ink-3);white-space:nowrap}
/* 面试单位**独占一整行**：它要能并排放下三个单位名 + 一个"继续添加"输入框，
   宽度不够就只能在框里换行，反而更难读。其余四项（日期/时段/方式/结论）
   在下一行并排 —— 这样不论窗口多宽，这块永远是整齐的两行，不会因为
   结论的提示文字长而被挤到第三行。 */
.ivrow .fld-wide{flex:1 1 100%;min-width:100%;align-self:stretch}
.ivrow input,.ivrow select{height:34px;padding:0 9px;font-size:14.5px;
  border:1px solid var(--line-2);border-radius:var(--r-s);background:var(--surface);color:var(--ink)}
.ivrow input[type="date"]{width:152px}
.ivrow input[type="datetime-local"]{width:196px}
.ivrow #ivSlot{width:130px}
/* 面试时间：固定时段下拉 + 手动输入并排；下拉按学历生成 30/50 分钟档。 */
.ivslot{display:flex;align-items:center;gap:5px;min-width:0}
.ivslot select{width:142px;height:34px;padding:0 5px;font-size:12.5px}
.ivslot #ivSlot{width:112px}
.ivrow #ivMode{width:118px}
.ivrow #ivPlace{width:170px}
.ivrow #obStatus{width:118px}
/* 面试单位：一个框里能并排放下三个单位名（每个约 5-7 字）+ 一个"继续添加"输入 */
.ivrow .multi{width:100%;min-height:34px;padding:4px 6px;gap:6px;
  border:1px solid var(--line-2);border-radius:var(--r-s);background:var(--surface)}
.ivrow .multi input{height:26px;border:0;background:transparent;padding:0 4px;min-width:150px;flex:1}
.ivrow .multi input:focus{outline:none}
/* 结论 = 徽标 + 下拉 + 「清空结论」，三者同一行（见 outcomeField 的说明） */
.ivrow .ivout{display:inline-flex;align-items:center;gap:8px;min-height:34px}
.ivrow .ivout .btn{height:34px;padding:0 11px}
.ivrow .mchip{font-size:13.5px;padding:3px 8px;border-radius:4px;background:var(--accent-soft);
  color:var(--accent-ink);border:1px solid var(--accent-line);white-space:nowrap}

/* 「quiet」区块：证据类内容压小一号，把强调留给档位 */
#detail .sec.quiet{margin-top:16px}
#detail .sec.quiet > h3{font-size:13px;letter-spacing:.04em}
#detail .sec.quiet .sum{font-size:14px;font-weight:500;line-height:1.6}
#detail .sec.quiet .ev{font-size:13px;line-height:1.6;padding:6px 9px;margin-top:6px}
#detail .sec.quiet .ev b{font-size:13px}
#detail .sec.quiet .skill span{font-size:13px;padding:2px 9px}
.skill .hit{color:var(--ok);background:var(--ok-soft);border-color:var(--ok-line)}
.skill .miss{color:var(--warn);background:var(--warn-soft);border-color:var(--warn-line)}
.skill .plain{color:var(--ink-2);background:var(--ground);border-color:var(--line)}
.ev{
  margin-top:8px;font-size:14px;color:var(--ink-2);background:var(--ground);
  border-left:2px solid var(--line-2);border-radius:0 4px 4px 0;padding:7px 10px;
}
.ev b{color:var(--ink);font-weight:600}
.sum{
  font-size:15px;color:var(--ink);font-weight:500;line-height:1.65;
}
.bar2{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:6px 0}
.lbl2{font-size:13px;color:var(--ink-3)}
/* 字段分组卡片：把"面试安排 / 面试结论 / 录用与入职"按语义分开，
   底色 + 组标题让一眼能看出这几项各管什么（业务方 2026-10-21 反馈：
   原来十几个控件堆在两行里，日期时段方式都没标签，看不出是干什么的）。*/
/* 分组只靠**一层底色**表达（业务方 2026-10-21 要求"按分类用底色分离"）。
   原来底色 + 1px 边框 + 圆角同时在说"这是一个组"，三重表达同一件事；
   底色已经够，边框去掉 —— 少一件装饰，分组关系反而更清楚。*/
.fgroup{background:var(--surface-2);border-radius:var(--r);padding:10px 12px;margin:8px 0}
.fgroup > h4{margin:0 0 8px;font-size:13px;font-weight:600;color:var(--ink-2)}
.frow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.frow + .frow{margin-top:8px}
.fld{display:inline-flex;align-items:center;gap:5px}
.fld > .fl{font-size:13px;color:var(--ink-3);white-space:nowrap}
/* 面试安排与结论放在同一块里左右排：左=事实记录，右=判断结果。 */
/* 面试提纲：问题按序号排、考察意图用灰字紧随其后（原来是一整段 JSON 原文） */
.ivqwrap{background:var(--surface-2);border-radius:var(--r);padding:10px 14px}
.ivqh{font-size:13px;font-weight:600;color:var(--ink-2);margin-bottom:8px}
.ivq{margin:0;padding-left:20px;display:grid;gap:11px}
.ivq li{font-size:14px;line-height:1.7;color:var(--ink)}
.ivq li::marker{color:var(--accent-ink);font-weight:600}
.ivq-q{color:var(--ink)}
.ivq-w{color:var(--ink-3);font-size:13px;margin-top:3px}
/* 操作记录：折叠头做成一条明确可点的横条（箭头 + 条数 + 底色），
   而不是一行灰字。展开才铺表格 —— 平时不该占版面，但必须看得出"这里能点开"。 */
.auditbox{border:1px solid var(--line-2);border-radius:var(--r-s);background:var(--surface)}
.auditbox > summary{
  list-style:none;cursor:pointer;display:flex;align-items:center;gap:7px;
  padding:7px 11px;font-size:13px;font-weight:600;color:var(--accent-ink);
  background:var(--accent-soft);border-radius:calc(var(--r-s) - 1px);
}
.auditbox > summary::-webkit-details-marker{display:none}
.auditbox > summary:hover{filter:brightness(.97)}
.audit-arrow{display:inline-block;transition:transform .15s ease;font-size:11px}
.auditbox[open] > summary{border-radius:calc(var(--r-s) - 1px) calc(var(--r-s) - 1px) 0 0}
.auditbox[open] .audit-arrow{transform:rotate(90deg)}
.audit-n{margin-left:auto;font-weight:600;color:var(--ink-2);font-size:12px}
.auditbox > table{width:100%;border-collapse:collapse;font-size:13px;padding:0}
.auditbox th{text-align:left;padding:7px 11px 4px;font-size:12px;font-weight:600;
  color:var(--ink-3);border-bottom:1px solid var(--line)}
.auditbox th:last-child,.auditbox td:last-child{width:80px}
.auditbox td{padding:5px 11px;border-bottom:1px solid var(--line);vertical-align:top}
.auditbox td.t{white-space:nowrap;color:var(--ink-2)}
.auditbox tbody tr:last-child td{border-bottom:0}
.auditbox #auditNote{padding:0 11px 8px;margin-top:0}
.iv-split{display:grid;grid-template-columns:1fr;
  gap:0;align-items:stretch}
/* 上下排列（业务方 2026-10-21）：左右两列时「面试安排」内容多占满左列、
   「面试结论」只有一行，右边空一大片，是卡片套件的通病 —— 格子对齐了但内容量不匹配。
   改成上下：各自按内容占高，中间用一条横线分隔。 */
.iv-col{min-width:0;padding:2px 0}
.iv-col + .iv-col{margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
.iv-col > h4{margin:0 0 8px;font-size:13px;font-weight:600;color:var(--ink-2)}
/* 教育经历紧贴姓名展示，不做折叠；第一学历与最高学历都保留编辑入口。 */
.edu-band{margin-top:12px;padding:11px 0 12px;border-top:1px solid var(--line);
  border-bottom:1px solid var(--line)}
.edu-band-head{display:flex;align-items:center;justify-content:space-between;gap:10px;
  margin-bottom:7px}
.edu-band-head h4{margin:0;font-size:13px;font-weight:600;color:var(--ink-2)}
.edu-list{display:grid;gap:4px}
.edu-line{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;
  min-height:22px;font-size:14px;padding:1px 0}
.edu-period{font-size:12px;color:var(--ink-3);white-space:nowrap;
  font-variant-numeric:tabular-nums;min-width:88px}
.edu-degree{font-size:13px;color:var(--ink-3)}
/* 荣誉/论文/专利：分类名用强调色加粗，条目名也加粗，出处保持常规 ——
   原来整行一个灰度，扫一眼分不出"是什么"和"凭什么"（业务方 2026-10-21）。 */
#detail .sec .ev b{color:var(--accent-ink);font-weight:650}
/* 每条荣誉做成一个浅色块：这篇档案里它是"加分事实"，不该和普通正文一个观感 */
.honosec .ev{background:var(--surface-2);border-radius:var(--r-s);
  padding:7px 11px;margin-top:6px;line-height:1.7}
/* 荣誉/论文/专利逐条展示：序号 + 自动换行，条目多时不再挤成一行 */
.honosec .honor-list{margin:5px 0 0;padding-left:22px;list-style:decimal}
.honosec .honor-list li{margin:2px 0;line-height:1.7;overflow-wrap:anywhere;
  word-break:break-word}
/* 项目经历（v4.0.18）：AI 短摘要，不是简历原文。
   方向总结用强调底高亮，项目本身一条一块；摘要固定两行，避免长文把档案拉得很长。 */
.projectsec .ai-tag{display:inline-block;margin-left:8px;padding:1px 7px;
  border-radius:999px;background:var(--warm-soft);border:1px solid var(--warm-line);
  color:var(--warm);font-size:11.5px;font-weight:600;vertical-align:2px}
.proj-dir{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;
  padding:8px 11px;margin:2px 0 10px;border-radius:var(--r-s);
  background:var(--warm-soft);border:1px solid var(--warm-line);line-height:1.65}
.proj-dir span{font-size:12px;color:var(--warm);font-weight:650;flex:none}
.proj-dir b{font-size:14.5px;color:var(--ink);font-weight:650}
.proj-list{display:flex;flex-direction:column;gap:8px}
.proj-item{padding:9px 12px;border-left:3px solid var(--accent-line);
  background:var(--surface-2);border-radius:0 var(--r-s) var(--r-s) 0}
.proj-name{font-size:14px;font-weight:650;color:var(--ink);line-height:1.5;
  overflow-wrap:anywhere}
.proj-sum{font-size:13.5px;color:var(--ink-2);line-height:1.65;margin-top:2px;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;
  overflow:hidden;overflow-wrap:anywhere}
.projectsec .proj-loading{padding:4px 0 2px}
/* 四六级那一行：标签名加粗，值用同一套淡灰可点控件 */
.honosec .cetrow b{color:var(--accent-ink);font-weight:650}
/* v5.1.1：未填时只占一行高度。原来 .edv 继承 1.7 行高 + 空值伪元素，
   在四六级/证书这一行会被撑成一个很高的浅蓝块（业务方截图反馈）。 */
.honosec .cetrow{display:flex;align-items:center;flex-wrap:wrap;gap:4px}
.honosec .cetrow .edv{vertical-align:middle;margin-left:2px;
  height:24px;min-height:24px;padding:0 8px;line-height:1.15}
.honosec .cetrow .edv.empty{height:24px;min-height:24px;line-height:1.15}
.honosec .cetrow .edv.empty::after{line-height:1}
.honosec .cetrow select{height:26px;min-height:26px;padding:0 8px;
  font-size:13.5px;line-height:1.2}
/* 教育经历里可点的学校/专业：虚下划线提示"这里能改"，与基本信息那套淡灰可点击一致 */
.edu-click{cursor:pointer;border-bottom:1px dashed transparent;border-radius:2px}
.edu-click:hover{background:var(--accent-soft);border-bottom-color:var(--accent-line);
  color:var(--accent-ink)}
.edu-school{font-weight:600;color:var(--ink);overflow-wrap:anywhere}
.edu-major{font-size:13px;color:var(--ink-2);overflow-wrap:anywhere}
.edu-tags{display:inline-flex;align-items:center;gap:5px;flex-wrap:wrap}
.edu-edit{display:grid;grid-template-columns:minmax(170px,1.25fr) 112px minmax(150px,1fr) minmax(140px,1fr);
  gap:10px;align-items:start;margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}
/* ---- 淡灰可点击的字段：点一下才变输入框 ----
   业务方 2026-10-21：档案里常驻一排编辑框，看着像"待填的表单"，
   可这些字段平时九成时候只是"看一眼"。默认显示淡灰文本、点击才进入编辑，
   页面立刻安静下来，需要改的字段再点开。 */
.edv{display:inline-flex;align-items:center;gap:4px;min-width:56px;padding:2px 8px;
  border:1px solid transparent;border-radius:4px;background:var(--surface-2);
  color:var(--ink-2);font-size:14px;cursor:text;max-width:100%}
.edv:hover{background:var(--accent-soft);color:var(--ink);border-color:var(--accent-line)}
.edv.empty{color:var(--ink-3)}
.edv.empty::after{content:'未填';font-size:13px}
.edv .edv-txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:240px}
.edu-field{display:grid;gap:4px;min-width:0}
/* ⚠️ 只作用于**第一个**子 span（那是字段名）。
     写成 `.edu-field > span` 会把里面的 `.edv`（淡灰可点击值）也套上
     11.5px 灰字 —— 值就比标签还小，看着像没内容（业务方 2026-10-21 截图）。 */
.edu-field > span:first-child{font-size:12px;color:var(--ink-3);line-height:1.4}
.edu-field input,.edu-field select{width:100%;min-width:0;height:32px;padding:0 9px;
  border:1px solid var(--line-2);border-radius:var(--r-s);background:var(--surface);font:inherit}
.edu-field input:focus,.edu-field select:focus{border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft);outline:none}
@media (max-width:1180px){
  /* .iv-split 现在默认就是上下单列，无需再覆盖；
     .edu-line 是 flex 内联，窄屏只把编辑组收成两列。 */
  .edu-edit{grid-template-columns:1fr 1fr}
  .detail-toolbar{padding:10px 18px}
  .detail-toolbar .dact{justify-content:flex-start}
  .ovrow,.ovgrid,.profile-layout{grid-template-columns:1fr}
  .ana-workbench{grid-template-columns:1fr}
  .ovgrid > .ov-span2,.profile-layout > .profile-wide,
  .profile-layout > .dupwarn,.profile-layout > .quality,
  .profile-layout > .edu-band{grid-column:auto}
}
/* ⚠️ 这条**必须限定在详情区**：写成裸 `textarea` 会把助手输入框也管住
   （max-width:760px → 助手框被压到 760 宽，发送键就跑到框外面去了）。 */
/* 行长控制：760px 在 14px 字号下约 54 个中文字，偏长（技能口径：正文 < 80 字符的换行节奏，
   中文按 45~60 字/行更稳）。收到 640px，备注这种自由文本读起来更顺。 */
#detail textarea{
  width:100%;max-width:640px;padding:8px 10px;border:1px solid var(--line-2);
  border-radius:var(--r-s);background:var(--surface);resize:vertical;font:inherit;outline:none;
}
textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:10px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:9px 12px}
.stat-n{font-size:21px;font-weight:600;line-height:1.2}
.stat-l{font-size:13px;color:var(--ink-2);margin-top:1px}
.stat-h{font-size:12px;color:var(--ink-3);margin-top:2px}
.todo{display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}
.todo:last-child{border-bottom:0}
.todo-i{color:var(--ink-3);font-size:12px;font-weight:600;min-width:14px;padding-top:2px}
.todo-t{font-size:14px;font-weight:600}
.todo-w{font-size:13px;color:var(--ink-2);margin-top:2px}
.todo-a{font-size:13px;color:var(--accent-ink);margin-top:2px}
.doc{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px dashed var(--line);font-size:14px}
.doc:last-child{border-bottom:0}
.doc-n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.doc-a{flex:none;display:flex;gap:6px}
.hint{color:var(--ink-3);font-size:14px}
.flagbad,.flagwarn,.flagneutral{display:inline-flex;align-items:center;border-radius:4px;
  padding:1px 6px;font-size:12px;line-height:1.55;white-space:nowrap}
.flagbad{color:var(--bad);background:var(--bad-soft);border:1px solid var(--bad-line)}
.flagwarn{color:var(--warn);background:var(--warn-soft);border:1px solid var(--warn-line)}
.flagneutral{color:var(--ink-2);background:var(--surface-2);border:1px solid var(--line)}
.quality{border-left:3px solid var(--warn);background:var(--warn-soft);padding:8px 11px;
  border-radius:5px;margin:8px 0;font-size:13px;color:var(--ink-2)}
.quality.bad{border-left-color:var(--bad);background:var(--bad-soft)}
.rawbox{margin-top:8px;border:1px solid var(--line);border-radius:6px;background:var(--surface)}
.rawbox > summary{cursor:pointer;padding:7px 10px;font-size:13px;color:var(--accent-ink);
  background:var(--accent-soft);border-radius:5px;font-weight:600}
.rawbox pre{margin:0;padding:10px 12px;max-height:420px;overflow:auto;white-space:pre-wrap;
  overflow-wrap:anywhere;background:var(--surface);font-size:12.5px;line-height:1.65}
.skill{display:flex;flex-wrap:wrap;gap:6px}
.skill > span{border:1px solid var(--line);background:var(--surface-2);
  border-radius:4px;padding:2px 7px;font-size:12.5px}
.skill .hit{color:var(--ok);background:var(--ok-soft);border-color:var(--ok-line)}
.skill .miss{color:var(--bad);background:var(--bad-soft);border-color:var(--bad-line)}
.skill .plain.unverified{color:var(--ink-3);background:transparent;border-style:dashed}
.toast{
  position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:260;
  background:var(--ink);color:#fff;padding:9px 16px;border-radius:8px;font-size:13px;
  opacity:0;pointer-events:none;transition:opacity .18s;
}
.toast.on{opacity:1}
/* 提示分四种语气（2026-10-20）。原来只有一个样式，于是 `toast(msg,'warn')`
   的第二个参数被**静默丢弃**——"保存失败"和"已保存"长得一模一样，
   出错时根本看不出来。写代码时传了 kind 却没生效，比根本不支持更坑。 */
.toast.ok{background:var(--ok-ink,var(--ok));color:#fff}
.toast.warn{background:var(--warn-soft);color:var(--warn);border:1px solid var(--warn-line)}
.toast.danger{background:var(--bad);color:#fff}
.toast.info{background:var(--ink);color:#fff}

/* ---------------- 页内确认弹窗 ----------------
   为什么不用浏览器原生 confirm()：原生那个是浏览器画的框，样式、字号、按钮文案都不可控，
   换台机器/换个浏览器长得就不一样，而且它挡住整个应用、看不出正在确认的是哪件事。
   HR 看到"网页弹出的框"而不是"这个系统的框"。 */
/* 普通业务弹窗必须压在设置弹窗之上：设置里的“新建模板 / 编辑模板 / 确认”
   打开的是同一个 `#mask`，如果它还在 60，就会被 settingsmask(120) 盖住。 */
.mask{position:fixed;inset:0;z-index:140;background:rgba(31,35,41,.36);display:none;
  align-items:center;justify-content:center}
.mask.on{display:flex}
.dlg{background:var(--surface);border-radius:10px;box-shadow:0 16px 48px rgba(31,35,41,.20);
  padding:22px 24px;width:min(560px,calc(100% - 48px))}
/* 宽体弹窗（编辑邮件模板用）：560px 装不下富文本编辑器 —— 正文区只剩一条缝，
   工具栏还要换行，等于没法排版。宽到 940px 并限高，内容超出在弹窗内滚动。 */
.dlg.wide{width:min(940px,calc(100% - 48px));max-height:calc(100vh - 56px);overflow-y:auto}
.dlg.wide .crow{display:block}
.dlg.wide .crow > label{display:block;width:auto;margin-bottom:4px;padding-top:0}
/* ⚠️ `.cinput` 是 flex 容器，只有 input/select/textarea 才带 `flex:1`；
   富文本编辑器是 `<div class="richeditor">`，拿不到那条规则 → 宽度塌成内容宽（实测 26px）。
   这里显式铺满，工具栏也一起占整行。 */
.dlg.wide .crow > .cinput{display:block;width:100%}
.dlg.wide .cinput > .mtool{width:100%;margin-bottom:6px}
.dlg.wide .cinput > .richeditor{width:100%;box-sizing:border-box;
  min-height:300px;max-height:46vh;overflow-y:auto}
.dlg.wide .cinput > input,.dlg.wide .cinput > select{width:100%;box-sizing:border-box}
.dlg h3{margin:0 0 12px;font-size:18px;font-weight:600;position:relative;padding-right:26px}
.dlg-x{position:absolute;right:0;top:-2px;font-size:21px;font-weight:400;color:var(--ink-3);
  cursor:pointer;line-height:1;padding:2px 5px;border-radius:4px}
.dlg-x:hover{color:var(--ink);background:var(--surface-2)}
.btn.busy{border-color:var(--accent-line);color:var(--accent-ink);background:var(--accent-soft)}
.dlg .db{line-height:1.8;font-size:15px;white-space:pre-wrap;color:var(--ink)}
.dlg .dbar{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}
/* 下载初筛简历：时间段选择（v4.0.19） */
.bundle-dates{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0}
.bundle-dates label{display:flex;flex-direction:column;gap:5px;font-size:13px;
  color:var(--ink-2);min-width:180px}
.bundle-dates input{height:36px;padding:0 10px;border:1px solid var(--line-2);
  border-radius:var(--r-s);background:var(--surface);font:inherit;color:var(--ink)}
.bundle-dates input:focus{border-color:var(--accent);outline:none;
  box-shadow:0 0 0 3px var(--accent-soft)}
.bundle-last{margin-top:4px;padding:9px 11px;border-radius:var(--r-s);
  background:var(--surface-2);color:var(--ink-2);font-size:13px;line-height:1.6}
/* ---- 按钮变体：**全部照搬原版**（ui.py 的同名规则）----
   原版的规矩（业务方 2026-10-14 定）：
     · 导入/收取组 = **浅蓝底深蓝字**（btn-primary）且**加大一档**（btn-lg）——每日常用动作；
     · 下载/导出组 = **浅绿底深绿字**（btn-green）——与导入组区分动作类别：**蓝=收进来，绿=发出去**；
     · 危险动作 = **白底灰边红字**（btn-danger），hover 才变红底 —— 一眼不吓人，但点之前清楚；
     · 主按钮一律浅底深字（HR 反馈"深底浅字看不清"）。 */
.btn-primary{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.btn-primary:hover{background:var(--accent-hi);border-color:var(--accent-ink);color:var(--accent-ink)}
.btn-green{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok)}
.btn-green:hover{background:var(--ok-hi);border-color:var(--ok);color:var(--ok)}
.btn-ok{background:var(--ok);border-color:var(--ok);color:var(--surface);font-weight:600}
.btn-ok:hover{background:var(--ok);border-color:var(--ok);color:var(--surface)}
.btn-danger{background:var(--surface);border-color:var(--line-2);color:var(--bad);font-weight:600}
.btn-danger:hover{background:var(--bad-soft);border-color:var(--bad);color:var(--bad)}
.btn-lg{padding:9px 18px;font-size:15px}

/* ---------------- 动效：只回应动作，不做装饰性入场 ----------------
   判断标准只有一条：**这段动画是不是在回答"你刚才做了什么"**。
     是 → 留：切换候选人（右栏淡入，表示"换人了"）、改档成功（档位按钮脉动）、
              选中行高亮条过渡（眼睛能跟上"选中的是谁"）。
     否 → 删：逐行 stagger 淡入、hover 放大、无限脉动 —— 这三样是"看起来像 AI 做的"
              页面的典型信号，对这个每天盯几小时的内部工具也只有干扰。
   全部尊重 prefers-reduced-motion。 */
@keyframes v2in{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
@keyframes v2pulse{0%{transform:scale(1)}45%{transform:scale(1.1)}100%{transform:scale(1)}}
/* ⚠️ 操作条**不参与**这个入场动画（2026-10-28）：v2in 的起始帧带
   `transform: translateY(5px)`，而 sticky 元素一旦有 transform，就会整体下移 5px ——
   表现为"常驻条上方露出 5px 的缝，下面的档案内容从缝里透出来"。 */
.detail.swap > *:not(.detail-toolbar){animation:v2in .17s cubic-bezier(.2,.7,.3,1) both}
.pulse{animation:v2pulse .32s ease-out}
@media (prefers-reduced-motion: reduce){
  .detail.swap > *, .pulse{animation:none!important}
  .row,.bar{transition:none!important}
  /* 登录页（v3.2）：位移/缩放/抖动全部去掉，**只留淡入淡出**。
     为什么还留淡入：它不引起前庭反应（没有位移），却仍然是"从无到有"的
     必要提示；全砍掉就退回原来的硬切了。星图的逐帧动画由 JS 侧关掉
     （见 startLoginNet 里的 NET_REDUCED 判断），这里管不到 canvas 内容。 */
  .loginwrap.enter .loginbox,.loginbox.shake,.loginerr.on{animation-name:loginFade!important}
  .loginbox.leaving{animation-name:loginFadeOut!important}
  .loginbtn.busy .loginico circle{animation:none!important}
  .loginbtn.done .loginico path{animation:none!important;stroke-dashoffset:0}
  body > .side,body > .main{transition:opacity .2s ease!important}
  body.locked > .side,body.locked > .main{transform:none!important}
}
@keyframes loginFade{from{opacity:0}to{opacity:1}}
@keyframes loginFadeOut{from{opacity:1}to{opacity:0}}

/* ---------------- 登录页（v3.0.3） ---------------- */
/* 只负责"拦住界面"。刻意不用卡片堆叠的营销式排版：这是一个每天要用、
   且只在登录时出现一次的东西，越平静越好。 */
/* 登录页背景：西北有色金属研究院大楼（v3.1.3）。
   三层叠出"融合"效果，而不是把图片压暗了事：
     ① 底图 —— 固定、cover、轻微放大，避免边缘露白；
     ② 白色蒙版 —— 从左上到右下由 .93 → .72 渐变，
        让登录框所在的中央区域更亮、四周稍暗，视线自然收到中间；
     ③ 一层极淡的品牌蓝斜向光晕，把画面和企业色连起来。
   `background-attachment:fixed` 在小窗口下会有滚动残影，这里不用它。 */
.loginwrap{
  position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;
  overflow:hidden;                 /* 星图 canvas 贴边，不让它把页面撑出滚动条 */
  transition:opacity .28s ease;    /* 退场时整层淡出，与登录框的放大同时进行 */
  background:
    linear-gradient(135deg, rgba(255,255,255,.93) 0%, rgba(255,255,255,.78) 45%,
                    rgba(238,244,255,.72) 100%),
    radial-gradient(circle at 72% 28%, rgba(51,112,255,.10), transparent 58%),
    url("/login-bg.jpg") center 42% / cover no-repeat,
    linear-gradient(160deg, #eef2f8 0%, #e6ecf6 100%);   /* 图缺失时的兜底 */
}
/* 窄屏（手机/小窗）：图会显得太挤，改成从顶部对齐并加深蒙版 */
@media (max-width: 720px){
  .loginwrap{background-position:center top, 72% 28%, center top, center;}
  .loginwrap{background-blend-mode:normal;}
}
/* 没登录：把整个应用区藏起来（遮罩之下的内容不该被看到/被读屏念出来）。
   用 opacity+transform 而不是原来的 visibility:hidden —— 后者是硬切，
   登录成功后没法淡入（见上面 body.locked 那条）。 */
.loginwrap[hidden]{display:none}
/* 人才星图（v3.2）：登录框之下的一层 canvas。
   注意层级：canvas 是 .loginwrap 的**子元素**，所以它一定画在 .loginwrap
   自己的背景（照片 + 白色渐变蒙版）**之上**——元素背景永远在子元素下面。
   换句话说，粒子是直接压在白色蒙版上画的，不会被蒙版压淡；
   "若隐若现"是靠代码里那几个低 alpha 值（点 .5、线 .3）做出来的，
   不是靠遮罩。改 alpha 时心里要有这个数。
   语义上它不是随便的科技感——这是个人才库，"点与点连成网"就是人才关系网。
   为什么用 canvas 而不是一堆 DOM 点：60 个点 + 每帧连线，DOM 方案要写
   上千个元素并每帧改样式，在这类办公机（集显/远程桌面）上必掉帧。 */
#loginNet{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:0}

/* ---- 登录框动效（v3.2）----
   三段，各回答一个问题：
     入场  "界面出来了"  → 淡入 + 上浮 8px（只动 opacity/transform，走合成层）
     失败  "密码不对"    → 横向轻抖，不用读字也知道错在哪
     成功  "进去了"      → 框放大淡出 + 整层淡出 + 应用区从 97% 推开
   入场只做一次（靠 .loginwrap.enter 类控制），401 把登录页叫回来时不重播 ——
   会话过期被弹回来时用户是着急的，这时候再让他等 280ms 是帮倒忙。 */
@keyframes loginIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes loginShake{
  0%,100%{transform:none}
  18%{transform:translateX(-5px)} 38%{transform:translateX(5px)}
  58%{transform:translateX(-3px)} 78%{transform:translateX(3px)}
}
@keyframes loginOut{
  from{opacity:1;transform:none}
  to{opacity:0;transform:scale(1.04)}
}
/* 登录框：在照片背景上要"浮起来"—— 用近乎不透明的白 + 更深的投影，
   不用 backdrop-filter（部分老显卡/远程桌面下会糊成一片）。 */
.loginbox{
  position:relative;z-index:1;      /* 压在星图之上 */
  width:min(340px,calc(100vw - 48px));padding:30px 28px 24px;
  background:rgba(255,255,255,.97);border:1px solid rgba(255,255,255,.9);
  border-radius:var(--r);
  box-shadow:0 18px 48px rgba(24,32,48,.18), 0 2px 8px rgba(24,32,48,.06);
}
.loginwrap.enter .loginbox{animation:loginIn .3s cubic-bezier(.2,.7,.3,1) both}
.loginbox.shake{animation:loginShake .24s cubic-bezier(.36,.07,.19,.97) both}
.loginbox.leaving{animation:loginOut .17s ease-in both;pointer-events:none}
.loginwrap.leaving{opacity:0}
/* 应用区：登录成功后从 97% 推开到 100%，和登录层的淡出叠在一起，
   观感是"推门进去"而不是"页面刷新了一下"。 */
body > .side,body > .main{transition:opacity .3s ease,transform .3s cubic-bezier(.2,.7,.3,1)}
body.locked > .side,body.locked > .main{opacity:0;transform:scale(.985);transform-origin:50% 40%}
.loginbrand{display:flex;align-items:center;gap:8px;font-size:17px;font-weight:650;
  color:var(--ink);letter-spacing:.01em}
.logindot{width:9px;height:9px;border-radius:2px;background:var(--accent);flex:none}
.loginsub{font-size:12.5px;color:var(--ink-3);margin:3px 0 22px;letter-spacing:.02em}
.loginlabel{display:block;font-size:12.5px;color:var(--ink-2);margin-bottom:5px}
.logininput{
  width:100%;box-sizing:border-box;height:38px;padding:0 11px;margin-bottom:14px;
  border:1px solid var(--line-2);border-radius:var(--r-s);background:var(--ground);
  font:inherit;font-size:14px;color:var(--ink);outline:none;
  transition:border-color .14s ease,box-shadow .14s ease,background .14s ease;
}
.logininput:focus{border-color:var(--accent);background:var(--surface);
  box-shadow:0 0 0 3px var(--accent-soft)}
.loginerr{min-height:18px;font-size:12.5px;color:var(--bad);margin:-4px 0 10px}
/* 错误行淡入：抖完之后"错在哪"再出现。用 .on 类触发而不是直接挂在元素上 ——
   挂在元素上只会在页面加载时播一次，之后每次改 textContent 都不会重播。 */
.loginerr.on{animation:loginIn .16s ease-out both}
.loginbtn{width:100%;height:38px;font-size:14.5px;
  display:flex;align-items:center;justify-content:center;gap:7px}
/* 按钮里的"忙碌"图标（v3.2）：旋转环 → 对勾，用同一条 SVG 路径做 morph。
   **不用文字"登录中…"代替它**：文字每换一次长度，按钮内容就会左右跳一下；
   这里固定 14px 见方，只在里面换形状。 */
.loginico{width:14px;height:14px;flex:none;display:none}
.loginico circle,.loginico path{
  fill:none;stroke:currentColor;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round;
}
/* r=6 → 周长 37.7；留 26 实线 + 12 空白，转起来才看得出方向 */
.loginico circle{stroke-dasharray:26 12;opacity:.9;
  transform-box:fill-box;transform-origin:50% 50%}
/* 对勾路径长 8.6，dasharray 给 14 保证完全盖住 */
.loginico path{stroke-dasharray:14;stroke-dashoffset:14}
.loginbtn.busy .loginico{display:block}
.loginbtn.busy .loginico circle{animation:spin .72s linear infinite}
.loginbtn.done .loginico{display:block}
.loginbtn.done .loginico circle{display:none}
.loginbtn.done .loginico path{animation:drawCheck .22s ease-out forwards}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes drawCheck{to{stroke-dashoffset:0}}
.loginhint{margin-top:14px;font-size:11.5px;color:var(--ink-3);text-align:center}
/* 首次使用提示：与普通脚注**同一副样子**（灰、小、居中），
   只是允许换行 —— 账号和密码挤在一行会糊成一片。
   刻意不做高亮块：登录框里只该有一个视觉重点（那个登录按钮）。 */
#loginFirstHint{
  margin-top:14px;font-size:11.5px;line-height:1.7;
  color:var(--ink-3);text-align:center;white-space:pre-line;
}
/* 顶栏上的"当前用户 + 退出"（登录模式才出现） */
.whoami{display:flex;align-items:center;gap:8px;flex:none;margin-left:6px;
  font-size:12.5px;color:var(--ink-3);white-space:nowrap}
.whoami b{color:var(--ink);font-weight:600}
.whoami button{border:0;background:transparent;color:var(--ink-3);cursor:pointer;
  font:inherit;font-size:12.5px;padding:2px 4px;border-radius:4px}
.whoami button:hover{background:var(--surface-2);color:var(--ink)}

/* ---------------- 账号管理（v3.0.3） ---------------- */
.uwrap{max-width:920px;padding:24px 28px 40px}
.uhead{display:flex;align-items:baseline;justify-content:space-between;
  gap:12px;margin-bottom:12px}
.uhead h3{margin:0;font-size:16px;font-weight:600;color:var(--ink)}
.utable{width:100%;border-collapse:collapse;font-size:13.5px}
.utable th{text-align:left;font-weight:600;font-size:12px;color:var(--ink-3);
  letter-spacing:.04em;padding:7px 10px;border-bottom:1px solid var(--line)}
.utable td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
.utable tr.off td{opacity:.55}
.usub{font-size:11.5px;color:var(--ink-3);margin-top:2px}
.utag{display:inline-block;font-size:10.5px;padding:1px 5px;border-radius:3px;
  margin-left:6px;vertical-align:1px}
.utag.me{background:var(--accent-soft);color:var(--accent-ink)}
.utag.off{background:var(--bad-soft);color:var(--bad)}
.pmtable td{font-size:12px}
/* 表头竖排：13 个权限横着放不下，写成两字一列竖着读反而清楚 */
.pmcol{display:inline-block;width:22px;text-align:center;font-size:11px;
  color:var(--ink-3);line-height:1.15;vertical-align:bottom;
  writing-mode:vertical-rl;letter-spacing:1px;padding-bottom:4px}
.pmask{display:inline-block;width:22px;text-align:center;font-size:13px;
  color:var(--line-2)}
.pmask.on{color:var(--ok);font-weight:700}

/* 只读角色：把"会改数据"的入口藏起来。
   **这是体验层的减法，不是安全边界** —— 服务端每个写接口都有 require()。
   藏掉的是那些对只读用户毫无意义、点了一定失败的按钮。 */
body.readonly .top2 .btn-primary,          /* 收取/导入/上传 */
body.readonly #btnArch,body.readonly #btnCompare,body.readonly .allbox,
body.readonly #btnBatch,
body.readonly .detail-toolbar .btn.primary,  /* 保存更正 */
body.readonly .detail-toolbar [onclick*="avatarRefresh"],
body.readonly .seg button,                   /* 档位按钮 */
body.readonly .stagesel,
body.readonly [data-act]{display:none!important}

/* ---------------- 前端体验优化（2026-11-02） ---------------- */
/* 列表加载态：一条顶部细进度条 + 列表/分页变淡，慢查询不再像"点了没反应" */
.loadbar{position:fixed;top:0;left:0;right:0;height:3px;z-index:80;overflow:hidden;
  pointer-events:none;opacity:0;transition:opacity .15s}
.loadbar.on{opacity:1}
.loadbar::before{content:'';display:block;width:35%;height:100%;background:var(--accent);
  border-radius:0 2px 2px 0;animation:loadslide 1s ease-in-out infinite}
@keyframes loadslide{0%{transform:translateX(-120%)}100%{transform:translateX(380%)}}
body.list-busy #list{opacity:.55;pointer-events:none}
body.list-busy #pager{opacity:.5;pointer-events:none}
/* 键盘可见焦点：鼠标点击不出现，Tab 才出现 */
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,
[tabindex]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.row:focus-visible{outline:2px solid var(--accent);outline-offset:-2px;background:var(--accent-soft)}
/* 窄屏：左栏抽成抽屉；侧栏只留图标 */
.listmask{display:none;position:fixed;inset:0;z-index:38;background:rgba(20,24,30,.18)}
#btnListToggle{display:none}
@media (max-width:1280px){
  body{grid-template-columns:168px 1fr}
  .wrap{grid-template-columns:340px 1fr}
  .search{width:230px}
  .detail{padding:0 18px 36px}
}
@media (max-width:1100px){
  body{grid-template-columns:64px 1fr}
  .side{padding:10px 6px}
  .sbrand{justify-content:center;padding:2px 0 12px}
  .sbrand span,.side button span,.side .sgroup,.side .sfoot .txt,.side .sfoot .chev{display:none}
  .side .sfoot .av{display:inline-flex}
  .side button{justify-content:center;padding:0}
  .wrap{grid-template-columns:1fr}
  .listwrap{position:fixed;left:64px;top:0;bottom:0;width:min(360px,82vw);z-index:40;
    transform:translateX(-115%);transition:transform .2s ease;
    box-shadow:8px 0 24px rgba(24,28,32,.16)}
  body.list-open .listwrap{transform:none}
  body.list-open .listmask{display:block}
  #btnListToggle{display:inline-flex}
  .detail{padding:0 14px 36px}
  .morePanel{max-width:calc(100vw - 90px)}
  /* 窄屏顶部工具条允许换行：1000px 下一行放不下搜索+筛选，硬塞会把
     "全部/未复核" 和工具栏按钮挤出可视区。 */
  .top{height:auto;min-height:52px;flex-wrap:wrap;gap:6px;padding:6px 10px}
  .top .search{flex:1 1 220px;width:auto;min-width:160px}
  #count{display:none}
}
/* ---------------- 招聘进度（v4.0.16） ---------------- */
.rp-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.rp-head h2{font-size:20px;margin:0 8px 0 0}
.rp-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-left:auto}
.rp-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:12px}
.rp-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  padding:16px 18px;position:relative;overflow:hidden}
.rp-card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:5px;background:var(--accent)}
.rp-card.c-blue::before{background:var(--accent)}
.rp-card.c-green::before{background:var(--ok)}
.rp-card.c-amber::before{background:var(--warn)}
.rp-card.c-red::before{background:var(--bad)}
.rp-card.c-purple::before{background:#7b61ff}
.rp-card .k{font-size:13px;color:var(--ink-3)}
.rp-card .v{font-size:32px;font-weight:800;line-height:1.15;margin-top:2px;
  font-variant-numeric:tabular-nums}
.rp-card.c-blue .v{color:var(--accent-ink)}
.rp-card.c-green .v{color:var(--ok)}
.rp-card.c-amber .v{color:var(--warn)}
.rp-card.c-red .v{color:var(--bad)}
.rp-card.c-purple .v{color:#6b4fe0}
.rp-card .s{font-size:12px;color:var(--ink-3);margin-left:6px;font-weight:400}
.rp-degrees{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
.rp-degree{background:var(--surface);border:1px solid var(--line);border-left:5px solid var(--line-2);
  border-radius:var(--r);padding:12px 14px}
.rp-degree .dt{font-size:16px;font-weight:700;margin-bottom:6px}
.rp-degree .dm{display:flex;gap:12px;flex-wrap:wrap;font-size:13px;color:var(--ink-2)}
.rp-degree .dm b{font-size:15px;color:var(--ink);font-variant-numeric:tabular-nums}
.rp-degree .gap.pos b{color:var(--warn)}
.rp-degree .gap.zero b{color:var(--ok)}
.rp-degree .gap.neg b{color:var(--bad)}
.rp-degree.d-doctor{border-left-color:#7b61ff}
.rp-degree.d-master{border-left-color:var(--accent)}
.rp-degree.d-bachelor{border-left-color:var(--ok)}
.rp-degree.d-college{border-left-color:#d97706}
/* v4.0.17：招聘进度主视觉（替代 8 个同构卡片） */
.recruit-hero{display:grid;grid-template-columns:210px minmax(0,1fr);gap:26px;
  background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  padding:20px 22px;margin-bottom:16px;position:relative;overflow:hidden}
.recruit-hero::after{content:"";position:absolute;right:-70px;top:-90px;width:260px;height:260px;
  border-radius:50%;background:radial-gradient(circle,var(--accent-soft),transparent 70%);
  pointer-events:none}
.rh-left{position:relative;z-index:1;display:flex;align-items:center;justify-content:center}
.rh-ring{position:relative;width:190px;height:190px}
.rh-ring svg{width:190px;height:190px;display:block}
.rh-ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:2px}
.rh-ring-center b{font-size:32px;font-weight:800;letter-spacing:-1px;font-variant-numeric:tabular-nums}
.rh-ring-center span{font-size:12px;color:var(--ink-3);white-space:nowrap}
.rh-right{position:relative;z-index:1;min-width:0}
.rh-kpis{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid var(--line);
  padding-bottom:14px;margin-bottom:14px}
.rh-kpi{padding:0 14px;border-left:1px solid var(--line)}
.rh-kpi:first-child{border-left:0;padding-left:0}
.rh-k{display:block;font-size:12px;color:var(--ink-3);white-space:nowrap}
.rh-v{display:block;font-size:26px;font-weight:800;line-height:1.25;font-variant-numeric:tabular-nums}
.rh-v.blue{color:var(--accent-ink)}
.rh-v.green{color:var(--ok)}
.rh-v.amber{color:var(--warn)}
.rh-v.red{color:var(--bad)}
.rh-v.purple{color:#6b4fe0}
.rh-degrees{display:grid;grid-template-columns:repeat(4,1fr);gap:0;
  border-top:1px solid var(--line);padding-top:12px}
.rh-degree{min-width:0;padding:0 14px;border-left:1px solid var(--line)}
.rh-degree:first-child{border-left:0;padding-left:0}
.rh-dhead{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--ink-2);
  margin-bottom:6px}
.rh-dot{width:9px;height:9px;border-radius:50%;flex:none}
.rh-dot.doctor{background:#7b61ff}
.rh-dot.master{background:var(--accent)}
.rh-dot.bachelor{background:var(--ok)}
.rh-dot.college{background:#d97706}
.rh-dname{font-weight:600;color:var(--ink);font-size:14px}
.rh-dstat{display:block;font-size:13px;color:var(--ink-3);margin-top:3px;
  white-space:nowrap;line-height:1.55}
.rh-dstat b{font-size:18px;font-variant-numeric:tabular-nums;color:var(--ink)}
.rh-dstat.gap.pos b{color:var(--warn)}
.rh-dstat.gap.zero b{color:var(--ok)}
.rh-dstat.gap.neg b{color:var(--bad)}
@media(max-width:900px){
  .recruit-hero{grid-template-columns:1fr;gap:16px}
  .rh-kpis{grid-template-columns:1fr 1fr;gap:12px 0}
  .rh-kpi:nth-child(3){border-left:0;padding-left:0}
  .rh-degrees{grid-template-columns:1fr 1fr;gap:14px 0}
  .rh-degree:nth-child(3){border-left:0;padding-left:0}
}
.rp-tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:12px}
.rp-tab{border:0;background:transparent;padding:8px 14px;color:var(--ink-3);border-bottom:2px solid transparent;font-size:14px}
.rp-tab.on{color:var(--accent-ink);border-bottom-color:var(--accent);font-weight:600}
.rp-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.rp-wrap{overflow:auto;background:var(--surface);border:1px solid var(--line);border-radius:var(--r);max-height:calc(100vh - 300px)}
.rp-table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px;min-width:980px}
.rp-table th{position:sticky;top:0;z-index:2;background:var(--surface-2);color:var(--ink-2);font-weight:600;font-size:12px;text-align:center;padding:9px 8px;border-bottom:1px solid var(--line-2);white-space:nowrap}
.rp-table th.plan{background:#fff8e1;color:#8a5a00}
.rp-table th.calc{background:#eef4ff;color:var(--accent-ink)}
.rp-table th.sign{background:#f3efff;color:#6b4fe0}
.rp-table td{padding:8px;border-bottom:1px solid var(--line);text-align:center;vertical-align:middle}
.rp-table tr.unit-start td{border-top:1px solid var(--line-2)}
.rp-table tr.has-sign td{background:#f6fcf8}
.rp-table td.unit{font-weight:600;background:var(--surface)}
.rp-unit-del{margin-left:6px;border:0;background:var(--bad-soft);color:var(--bad);
  border-radius:4px;font-size:12px;line-height:1.4;padding:1px 6px;cursor:pointer}
.rp-unit-del:hover{background:var(--bad);color:#fff}
.rp-table td.num{font-variant-numeric:tabular-nums}
.rp-table td.gap-pos{color:var(--warn);font-weight:600}
.rp-table td.gap-neg{color:var(--bad);font-weight:600}
.rp-table td.signed-pos{color:var(--ok);font-weight:700}
.rp-table td.rate-good{color:var(--ok);font-weight:600}
.rp-table td.rate-mid{color:var(--accent-ink);font-weight:600}
.rp-table td.rate-zero{color:var(--ink-3)}
.rp-table td.rate-none{color:var(--ink-3)}
.rp-table input.rp-num{width:64px;height:30px;text-align:center;border:1px solid var(--line-2);border-radius:4px;padding:0 4px}
.rp-signee{display:inline-block;margin:2px 3px 2px 0;padding:2px 7px;border-radius:999px;background:var(--accent-soft);color:var(--accent-ink);cursor:pointer;font-size:12px}
.rp-signee:hover{background:var(--accent-hi)}
.rp-empty{background:var(--surface);border:1px dashed var(--line-2);border-radius:var(--r);padding:36px 20px;text-align:center;color:var(--ink-3)}
.rp-health{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.rp-health-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:12px 14px}
.rp-health-card h4{margin:0 0 6px;font-size:14px;display:flex;justify-content:space-between;gap:8px}
.rp-health-card .n{color:var(--warn);font-weight:700}
.rp-health-item{padding:6px 0;border-top:1px dashed var(--line);font-size:13px;display:flex;gap:8px;align-items:center;justify-content:space-between}
.rp-health-item .m{color:var(--ink-3);font-size:12px}
.rp-health-item .a{display:flex;gap:6px;flex-shrink:0}
.rp-import-table{width:100%;border-collapse:collapse;font-size:12.5px}
.rp-import-table th,.rp-import-table td{border-bottom:1px solid var(--line);padding:5px 6px;text-align:center}
.rp-form{display:flex;flex-direction:column;gap:10px}
.rp-form label{font-size:13px;color:var(--ink-3);margin-bottom:-4px}
.rp-form > input{height:34px;border:1px solid var(--line-2);border-radius:6px;padding:0 10px;font-size:14px}
.rp-form > input:focus{outline:none;border-color:var(--accent)}
.rp-date-range{display:flex;align-items:center;gap:8px}
.rp-date-range input{height:34px;border:1px solid var(--line-2);border-radius:6px;padding:0 10px;
  font-size:14px;width:170px}
.rp-date-range input:focus{outline:none;border-color:var(--accent)}
.rp-date-range .dash{color:var(--ink-3);user-select:none}
@media(max-width:900px){.rp-cards,.rp-degrees{grid-template-columns:repeat(2,1fr)}}

/* ---------------- 日程安排（v4.4.3） ----------------
   日历只读「待面试」阶段；拖拽只改面试日期/时刻，不改阶段。 */
.sched-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px;flex:none}
.sched-title{font-size:20px;font-weight:700;letter-spacing:-.01em;line-height:1.3}
.sched-sub{font-size:13px;color:var(--ink-3)}
.sched-tools{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.sched-body{flex:1;min-height:0;overflow:auto;padding-bottom:8px}
.sched-month{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));border:1px solid var(--line);
  border-radius:10px;overflow:hidden;background:var(--surface);min-width:700px}
.sched-dow{padding:8px 9px;font-size:12.5px;font-weight:600;color:var(--ink-3);
  background:var(--surface-2);border-left:1px solid var(--line)}
.sched-dow:first-child{border-left:0}
.sched-cell{min-height:118px;padding:5px 6px 6px;border-top:1px solid var(--line);
  border-left:1px solid var(--line);min-width:0;position:relative;transition:background .12s}
.sched-cell:nth-child(7n+1){border-left:0}
.sched-cell.out{background:var(--surface-2)}
.sched-daynum{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;
  padding:0 5px;border-radius:999px;font-size:12.5px;font-weight:600;color:var(--ink-2)}
.sched-cell.out .sched-daynum{color:var(--ink-3);opacity:.65}
.sched-cell.today .sched-daynum{background:var(--accent);color:#fff}
.sched-ev{display:block;width:100%;text-align:left;font:inherit;font-size:12.5px;line-height:1.3;
  margin-top:3px;padding:3px 6px;border:0;border-left:3px solid var(--accent);border-radius:3px;
  background:var(--accent-soft);color:var(--ink);cursor:pointer;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;transition:filter .12s,box-shadow .12s}
.sched-ev:hover{filter:brightness(.97);box-shadow:0 0 0 1px var(--accent-line)}
.sched-ev.dragging{opacity:.42}
.sched-ev .t{font-weight:700;margin-right:0;color:var(--accent-ink)}
.sched-ev .meta{display:inline;font-size:11px;font-weight:400;color:var(--ink-2);
  white-space:normal}
/* 月视图格子窄：让“08:00-08:30”先占一行，姓名不够宽时自然换到第二行，
   时间范围始终完整可见（周/日视图仍是单行块）。 */
.sched-cell .sched-ev{white-space:normal;text-overflow:clip;line-height:1.25;
  font-size:11.5px;padding:2px 4px}
.sched-cell .sched-ev .t{margin-right:0}
.sched-more{display:block;margin-top:2px;padding:0 2px;border:0;background:none;color:var(--ink-3);
  font:inherit;font-size:11.5px;cursor:pointer;text-align:left}
.sched-more:hover{color:var(--accent-ink)}
.sched-cell.drag-over{background:var(--accent-soft);outline:2px dashed var(--accent);outline-offset:-3px}
.sched-empty{padding:28px;text-align:center;color:var(--ink-3);font-size:14px}
/* 有并发的日列会被 schedDayWidth 加宽；超出时整张周表横向滚动，
   表头和表体在同一个滚动容器里，不会错位。 */
.sched-week{min-width:760px;border:1px solid var(--line);border-radius:10px;
  overflow-x:auto;overflow-y:hidden;background:var(--surface)}
.sched-weekhead{display:flex;background:var(--surface-2);border-bottom:1px solid var(--line)}
.sched-weekhead .sched-timecol{border-right:1px solid var(--line)}
.sched-weekhead .sched-dayhead{flex:1;min-width:92px;padding:7px 8px;font-size:12.5px;
  font-weight:600;color:var(--ink-2);border-left:1px solid var(--line)}
.sched-weekhead .sched-dayhead:first-of-type{border-left:0}
.sched-weekhead .sched-dayhead.today{color:var(--accent-ink);background:var(--accent-soft)}
.sched-weekbody{display:flex}
.sched-timecol{width:58px;flex:none;color:var(--ink-3);font-size:11.5px;text-align:right;
  padding-right:6px;background:var(--surface);position:relative}
.sched-allday-label{height:34px;line-height:34px;border-bottom:1px solid var(--line)}
.sched-hourlabel{height:46px;position:relative;top:-7px}
.sched-endtime{position:absolute;left:0;right:6px;bottom:0;line-height:1;
  transform:translateY(-2px);text-align:right;color:var(--ink-3)}
.sched-daycol{flex:1;min-width:92px;position:relative;border-left:1px solid var(--line)}
.sched-daycol:first-child{border-left:0}
.sched-allday{min-height:34px;border-bottom:1px solid var(--line);padding:2px 3px}
.sched-allday .sched-ev{margin-top:2px;padding:1px 4px;font-size:11.5px;line-height:1.25}
.sched-hours{position:relative}
.sched-hline{height:46px;border-top:1px solid var(--line)}
.sched-hline:nth-child(2n){background:rgba(127,127,127,.025)}
.sched-ev.timed{position:absolute;left:3px;right:3px;margin:0;z-index:1;
  font-size:13px;line-height:1.15;padding:1px 6px;
  white-space:normal;overflow:hidden;min-height:19px}
.sched-daycol.drag-over{background:var(--accent-soft)}
.sched-week.single{min-width:0}
.sched-week.single .sched-daycol{min-width:0}
/* 日视图是单列，空间足够：时间/姓名/单位/地点统一放大两号（15px），
   行高压紧以容纳 30 分钟的卡片高度。 */
.sched-week.single .sched-ev.timed{font-size:15px;line-height:1.05;padding:0 6px}
.sched-week.single .sched-ev .meta{font-size:15px}
.sched-readonly .sched-ev{cursor:pointer}


/* ============================================================================
   v4.1 — Warm Minimal design system (Anthropic / Claude-inspired)
   ----------------------------------------------------------------------------
   This block intentionally sits last: it re-skins the existing business pages
   through shared tokens and base components without changing any DOM contract,
   route, permission or data path. Business logic stays frozen.
   ============================================================================ */
::selection{background:var(--accent-soft);color:var(--ink)}
*{scrollbar-color:var(--line-2) transparent;scrollbar-width:thin}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:999px}
a{color:var(--accent-ink)}
.loadbar{background:var(--accent)}
h1,h2,h3,h4{letter-spacing:-.01em}

/* ---- Navigation / chrome ------------------------------------------------ */
body{grid-template-columns:232px 1fr}
.side{padding:20px 12px 12px;background:var(--nav);border-right:1px solid var(--line)}
.sbrand{padding:2px 12px 18px;gap:10px;font-size:15px;letter-spacing:0}
.sbrand img{width:28px;height:28px;border-radius:8px;box-shadow:none}
.sbrand b{font-weight:600;letter-spacing:0}
.sgroup{padding:16px 12px 7px;font-size:10.5px;font-weight:600;
  letter-spacing:.12em;color:var(--ink-3)}
.side button{height:38px;padding:0 11px;gap:11px;border-radius:8px;
  font-size:13.5px;color:var(--ink-2);
  transition:background-color .18s ease,color .18s ease,box-shadow .18s ease}
.side button .ico{width:18px;height:18px;opacity:.62;stroke-width:1.55}
.side button:hover{background:var(--surface-2);color:var(--ink)}
.side button:hover .ico{opacity:.85}
.side button.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
.side button.on .ico{opacity:1;stroke-width:1.7}
.sfoot{padding:10px 2px 2px}
.acct{height:44px;border-radius:8px;transition:background-color .18s ease}
.acct:hover{background:var(--surface-2)}
.acctMenu{border-radius:10px;box-shadow:var(--shadow);padding:5px}
.acctMenu button{border-radius:6px}
.acctMenu .ver{font-size:10.5px;letter-spacing:.02em}

.top{height:56px;padding:0 22px;gap:9px;background:var(--surface);
  border-bottom:1px solid var(--line)}
.top2{padding:9px 22px;gap:7px;background:var(--surface);
  border-bottom:1px solid var(--line)}
.tabbar{padding:0 22px;background:var(--surface);border-bottom:1px solid var(--line)}
.brand{font-size:14px;font-weight:600}
.brand i{width:7px;height:7px;border-radius:2px;background:var(--accent)}
.search{width:280px;height:34px;border-radius:7px;border-color:transparent;
  background:var(--surface-2);font-size:13px;
  transition:background-color .18s ease,border-color .18s ease,box-shadow .18s ease}
.search:hover{background:var(--surface-2);border-color:var(--line)}
.search:focus{background:var(--surface);border-color:var(--accent-line);
  box-shadow:0 0 0 3px var(--accent-soft)}
.sel,.msel > .msbtn{height:34px;border-radius:7px;border-color:var(--line);
  background:var(--surface);color:var(--ink-2);font-size:13px}
.sel:hover,.msel > .msbtn:hover{border-color:var(--line-2);color:var(--ink)}
.count{font-size:12px;color:var(--ink-3)}
.count b{font-size:14px;font-weight:600}
.vdiv{background:var(--line)}
.mchip,.tochip{border-radius:999px}

/* ---- Buttons / forms ---------------------------------------------------- */
.btn{height:32px;padding:0 13px;border-radius:7px;border-color:var(--line);
  background:var(--surface);font-size:13px;font-weight:500;
  transition:background-color .18s ease,border-color .18s ease,color .18s ease}
.btn:hover{background:var(--surface-2);border-color:var(--line-2);color:var(--ink)}
.btn.primary{background:var(--accent-soft);border-color:var(--accent-line);
  color:var(--accent-ink);font-weight:600}
.btn.primary:hover{background:var(--accent-hi);border-color:var(--accent);
  color:var(--accent-ink)}
.btn-primary{background:var(--accent-soft);border-color:var(--accent-line);
  color:var(--accent-ink);font-weight:600}
.btn-primary:hover{background:var(--accent-hi);border-color:var(--accent);
  color:var(--accent-ink)}
.btn-green{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok)}
.btn-green:hover{background:var(--ok-hi);border-color:var(--ok-line);color:var(--ok)}
.btn.danger{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}
.btn.danger:hover{background:var(--bad-soft);border-color:var(--bad);color:var(--bad)}
.btn-lg{height:34px;padding:0 14px;font-size:13.5px}
.top2 .btn-lg{height:32px;padding:0 12px;font-size:12.5px}
.btn:disabled{opacity:.42}
.btn:focus-visible,.side button:focus-visible,.row:focus-visible,
.tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.seg{border-radius:8px;overflow:hidden}
.seg button{font-size:12.5px;padding:6px 14px;transition:background-color .18s ease}
.seg button.on{background:var(--accent-soft);color:var(--accent-ink);
  box-shadow:inset 0 0 0 1px var(--accent-line)}
input:not([type=checkbox]):not([type=radio]),select,textarea{
  border-color:var(--line);border-radius:7px;background:var(--surface);
  transition:border-color .18s ease,box-shadow .18s ease,background-color .18s ease}
input:not([type=checkbox]):not([type=radio]):hover,select:hover,textarea:hover{
  border-color:var(--line-2)}
input:not([type=checkbox]):not([type=radio]):focus,select:focus,textarea:focus{
  outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
input[type=checkbox],input[type=radio]{accent-color:var(--accent)}

/* ---- Candidate list ----------------------------------------------------- */
.wrap{grid-template-columns:380px 1fr}
.listwrap{background:var(--surface);border-right:1px solid var(--line)}
.row{padding:11px 14px 11px 0;gap:9px;border-bottom-color:var(--line);
  transition:background-color .18s ease}
.row:hover{background:var(--surface-2)}
.row.on{background:var(--accent-soft)}
.row .nm{font-size:14px;font-weight:600}
.row .sub{font-size:12.5px;color:var(--ink-3)}
.rowbar{width:2px;min-height:38px;border-radius:0}
.row.on .rowbar{background:var(--accent)}
.av{background:var(--surface-2);color:var(--ink-3);font-size:11px}
.pager{background:var(--surface);border-top-color:var(--line);font-size:12px}
.tier{border-radius:5px;font-weight:600}

/* ---- Detail / content --------------------------------------------------- */
.detail{padding:0 30px 48px;background:var(--surface)}
.detail-toolbar{background:var(--surface);border-bottom:1px solid var(--line);
  box-shadow:none;padding:16px 0 12px}
.dname{font-size:24px;font-weight:600;letter-spacing:-.015em}
.dmeta{font-size:14px;color:var(--ink-2);gap:7px 10px}
#detail .dmeta .chipl{font-size:13.5px}
.dmeta .edv{font-size:14px;background:var(--surface-2)}
.dmeta.dmeta2 .stagechip{background:var(--surface-2);border-color:var(--line);
  color:var(--ink)}
.av-lg{border-radius:10px;background:var(--surface-2)}
.sec{margin-top:24px}
.sec > h3,#detail .sec > h3{font-size:16px;font-weight:600;letter-spacing:0;
  color:var(--ink);border-bottom:1px solid var(--line);padding-bottom:8px;
  margin-bottom:14px}
.sec.tier-sec > h3{font-size:16px;border-bottom:0;padding-bottom:0}
.profile-layout{font-size:14px;gap:0 28px}
.profile-layout .ev,#detail .ev{font-size:14px;line-height:1.75}
.profile-layout .sum,#detail .sum{font-size:15px;line-height:1.75}
.profile-layout .edu-line{font-size:14px}
.profile-layout .hint{font-size:12.5px}
.honosec .ev{background:var(--surface-2);border-radius:7px}
.chipl,.jchip,.rk,.stg,.ochip{border-radius:999px;font-weight:500}
.chipl{font-size:12px;padding:3px 9px}
.rk{font-size:12px;padding:2px 8px}
.srcbar{background:var(--ok-soft);border-color:var(--ok-line)}
.srcbar > .srck{color:var(--ok)}
.srcbar .srcsub,.srcbar .srcfile .fn{color:var(--ok)}
.srcbar .srcmeta{color:var(--ok)}
.srcbar .srcfile{border-left-color:var(--ok-line)}
.srcbar > .srcfiles{border-left-color:var(--ok-line);border-top-color:var(--ok-line)}
.srcbar .btn{background:var(--surface);border-color:var(--ok-line);color:var(--ok)}
.srcbar .btn:hover{background:var(--ok);border-color:var(--ok);color:var(--surface)}
.btn.reviewed{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok)}
.btn.reviewed:hover{background:var(--ok-hi);border-color:var(--ok);color:var(--ok)}
.pv{background:var(--surface)}
.auditbox{border-color:var(--line);border-radius:8px}
.auditbox th{background:var(--surface-2)}
.mdtable th{background:var(--surface-2);font-weight:600}
.mdtable tbody tr:nth-child(even){background:transparent}
.mdtable tbody tr:hover{background:var(--surface-2)}

/* ---- Overview / stats / recruit ---------------------------------------- */
.ovcol,.rp-card,.rp-degree,.recruit-hero,.rp-wrap,.rp-health-card,
.ana-workbench,.ov-analysis,.stat{background:var(--surface);border-color:var(--line);
  border-radius:10px;box-shadow:none}
.ovcol{padding:18px 20px}
.ovcol > h3{font-size:16px;font-weight:600;padding-bottom:10px}
.rp-card{padding:18px 20px}
.rp-card::before{display:none}
.rp-card .v{font-size:30px;font-weight:650;letter-spacing:-.02em}
.rp-card .k{font-size:12.5px;color:var(--ink-3)}
.rp-degree{border-left-width:2px;padding:14px 16px;border-radius:8px}
.rp-degree .dt{font-size:15px;font-weight:600}
.rp-degree.d-doctor{border-left-color:var(--plum)}
.rp-degree.d-college{border-left-color:var(--warn)}
.recruit-hero{padding:22px 24px;border-radius:10px}
.recruit-hero::after{display:none}
.rh-ring-center b{font-size:30px;font-weight:650}
.rh-ring-track{stroke:var(--surface-2)}
.rp-card.c-purple::before{display:none}
.rp-card.c-purple .v,.rh-v.purple{color:var(--plum)}
.rh-dot.doctor{background:var(--plum)}
.rh-dot.college{background:var(--warn)}
.rh-kpis{border-bottom-color:var(--line)}
.rh-degrees{border-top-color:var(--line)}
.rh-degree{border-left-color:var(--line)}
.rp-wrap{border-radius:10px}
.rp-table{font-size:14.5px}
.rp-table th{background:var(--surface-2);color:var(--ink-2);font-size:12.5px;
  letter-spacing:.02em;font-weight:600;padding:11px 9px;border-bottom-color:var(--line-2)}
.rp-table th.plan{background:var(--warn-soft);color:var(--warn)}
.rp-table th.calc{background:var(--accent-soft);color:var(--accent-ink)}
.rp-table th.sign{background:var(--plum-soft);color:var(--plum)}
.rp-table td{padding:11px 9px;border-bottom-color:var(--line)}
/* 每个单位交替底色，扫表时能一眼分出单位边界（v4.3.0） */
.rp-table tbody tr.unit-even td{background:var(--row-alt)}
.rp-table tbody tr.unit-even td.unit{background:var(--row-unit);font-weight:650}
.rp-table tbody tr.unit-even.has-sign td{background:#f3faf6}
.rp-table tbody tr.unit-even.has-sign td.unit{background:#e7f4ea}
.rp-table .rp-signee{font-size:13.5px}
.rp-table .rp-signee .rp-deadline{font-style:normal;color:var(--ink-3);font-size:12px;margin-left:1px}
/* 签约人全部展示、不折叠：单元格保持单行，表格横向滚动查看；
   前三列（序号/单位/学历）固定，滚动时始终可见。 */
.rp-table td.signee-cell{text-align:left;white-space:nowrap;min-width:420px}
.rp-table .sticky-seq,.rp-table .sticky-unit,.rp-table .sticky-edu{
  position:sticky;z-index:4;background:var(--surface)}
.rp-table th.sticky-seq,.rp-table th.sticky-unit,.rp-table th.sticky-edu{z-index:7}
.rp-table .sticky-seq,.rp-table .sticky-unit,.rp-table .sticky-edu{white-space:nowrap}
.rp-table .sticky-seq{left:0}
.rp-table .sticky-unit{left:var(--freeze1,52px)}
.rp-table .sticky-edu{left:var(--freeze2,148px);box-shadow:2px 0 0 var(--line)}
.rp-table tbody tr.unit-even .sticky-unit{background:var(--row-unit)}
.rp-table tbody tr.unit-even .sticky-seq,
.rp-table tbody tr.unit-even .sticky-edu{background:var(--row-alt)}
/* 鼠标悬停必须有变化：交替底色/已签底色的选择器更具体，
   普通 tr:hover 会被盖掉，这里显式提高优先级。 */
.rp-table tbody tr:hover td,
.rp-table tbody tr.unit-even:hover td,
.rp-table tbody tr.unit-odd:hover td,
.rp-table tbody tr.has-sign:hover td{background:var(--surface-2)!important}
.rp-table tr.has-sign td{background:var(--ok-soft)}
.rp-table td.unit{background:var(--surface)}
.rp-table input.rp-num{height:30px;border-radius:6px}
.rp-signee{background:var(--accent-soft);color:var(--accent-ink);font-size:11.5px}
.rp-signee:hover{background:var(--accent-hi)}
.rp-health-card h4{font-size:14px}
.rp-tab{font-size:13px;padding:9px 14px;font-weight:500}
.rp-tab.on{font-weight:600}
.rp-head h2{font-size:24px;font-weight:600;letter-spacing:-.015em}
.rp-empty{border-color:var(--line-2);background:var(--surface)}
.ovjob-track{height:7px;background:var(--surface-2)}
.ovjob-track i{background:var(--accent)}
.ovjob-head{font-size:12px}
.ovjob{margin-bottom:12px}
.lgitem{border-radius:5px;font-size:12.5px}
.lgitem:hover{background:var(--surface-2)}
.tier-donut svg .td-seg{transition:stroke-width .18s ease}
.tier-donut svg .td-seg:hover{stroke-width:17}
.stat{padding:12px 14px}
.stat-n{font-size:28px;font-weight:600;letter-spacing:-.01em}
.stat-l{font-size:12.5px;color:var(--ink-2)}
.stat-h{font-size:11.5px;color:var(--ink-3)}
.stats{gap:12px}

/* ---- Mail / editor / system pages -------------------------------------- */
#sysView.on{padding:26px 30px 48px}
#sysView > *{max-width:1080px}
#recruitView.on{padding:26px 30px 48px}
#mailView.on,#orgView.on,#archView.on,#chatView.on{background:var(--surface)}
.medit{padding:22px 30px 0;background:var(--surface)}
.mrow > label{font-size:12.5px;color:var(--ink-3)}
.richeditor,#mBody{border-color:var(--line);background:var(--surface)}
.cw-mark{width:44px;height:44px;border-radius:12px;background:var(--accent-soft);
  color:var(--accent-ink);box-shadow:none}
.cw-card{border-radius:9px;transition:background-color .18s ease,border-color .18s ease}
.cw-card:hover{background:var(--surface-2);border-color:var(--line-2)}
.chatconv{border-right-color:var(--line)}
.chatconv-item{border-radius:7px}
.chatconv-item:hover{background:var(--surface-2)}
.chatconv-item.on{background:var(--accent-soft)}
.chatinput{background:var(--surface);border-top-color:var(--line)}
.chatside{background:var(--surface);border-left-color:var(--line)}
.msg{border-radius:9px;font-size:15.5px;line-height:1.75}
/* 用户自己发的消息靠右、浅蓝底，和助手的白色回答分开（2026-11-05）。
   width:fit-content 让短句只占一句话的宽度，长句到 680px 再换行；
   margin-left:auto 是右对齐的关键，只改颜色不改布局的话仍然会贴在左边。 */
.msg.user{display:block;width:fit-content;max-width:min(100%,680px);
  margin:0 0 12px auto;background:#e8f1ff;border-color:#cfe0f7;
  border-bottom-right-radius:3px}
.msg.assistant{background:var(--surface);border-color:var(--line);
  border-bottom-left-radius:3px}
/* 回答里的结构：小标题加粗、段间距拉开，长答案不再是一整块文字 */
#chatView .msg h3{font-size:16.5px;font-weight:700;margin:14px 0 7px;
  letter-spacing:-.01em;line-height:1.5}
#chatView .msg h4{font-size:15.5px;font-weight:700;margin:12px 0 6px;line-height:1.55}
#chatView .msg p{margin:0 0 8px}
#chatView .msg p:last-child{margin-bottom:0}
#chatView .msg ul,#chatView .msg ol{margin:6px 0 10px;line-height:1.75}
#chatView .msg li{margin:3px 0}
#chatView .msg strong{font-weight:700;color:var(--ink)}
#chatView .msg .mdtable{font-size:14px}
#chatView .msg blockquote{margin:8px 0;padding:6px 12px}
.ask{border-radius:999px;border-color:var(--line);font-size:12.5px}
.ask:hover{background:var(--surface-2);border-color:var(--line-2);color:var(--ink)}
/* Empty chat: put the welcome block in the optical centre of the canvas
   instead of leaving a tall band of dead space under it. */
.chatlog:has(.chatwelcome){display:flex;flex-direction:column;justify-content:center}
.chatlog:has(.chatwelcome) .chatwelcome{margin:0 auto;padding-bottom:5vh}
.chatwelcome h2{font-size:26px;font-weight:600;letter-spacing:-.03em}
.chatwelcome p{color:var(--ink-2);font-size:13px}
.cw-card{background:var(--surface);border-color:var(--line)}
.cw-card:hover{background:var(--surface-2);border-color:var(--line-2)}
.cw-brief{margin-top:18px;display:flex;flex-direction:column;gap:6px;align-items:flex-start}
.cw-brief-h{font-size:12px;color:var(--ink-3);margin-bottom:2px}
.cw-brief-card{align-items:flex-start;text-align:left}
.cw-w{display:block;font-size:12px;color:var(--ink-3);margin-top:3px;line-height:1.5}

/* 智能助手页：Claude Code 暖色（第一版）。只作用于聊天内容区；
   左侧主导航与会话列表的“选中/点击”统一用原始蓝色，避免同一列里两种强调色。 */
#chatView .chatwrap{
  --accent:#C56A45; --accent-ink:#A34E2F; --accent-soft:#F1EAE5;
  --accent-line:#E4D2C7; --accent-hi:#EADDD4;
}
:root[data-theme="dark"] #chatView .chatwrap{
  --accent:#D98A65; --accent-ink:#E7A985; --accent-soft:#382F29;
  --accent-line:#5A4438; --accent-hi:#42352E;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]) #chatView .chatwrap{
    --accent:#D98A65; --accent-ink:#E7A985; --accent-soft:#382F29;
    --accent-line:#5A4438; --accent-hi:#42352E;
  }
}
/* 用户气泡固定浅蓝，不跟随助手页的 Claude 暖色：暖色画布 + 浅蓝气泡，
   一眼能分清“我说的”和“助手答的”。 */
:root[data-theme="dark"] .msg.user{background:#1c2f47;border-color:#2f4d70}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]) .msg.user{background:#1c2f47;border-color:#2f4d70}
}
/* 会话列表选中态：与左侧主导航同一套蓝色，不再跟着聊天内容变暖色 */
.chatconv-item.on{background:var(--sel);color:var(--sel-ink)}
.chatconv-item.on .cc-title{color:var(--sel-ink)}
#chatView .chatlog:has(.chatwelcome){display:block}
#chatView .chatlog:has(.chatwelcome) .chatwelcome{margin:4vh auto 0;padding-bottom:20px}
#chatView .chatwelcome h2{font-size:27px;line-height:1.3;font-weight:650;letter-spacing:-.4px;margin:0 0 8px}
#chatView .chatwelcome p{font-size:14px;line-height:1.65;color:var(--ink-3);max-width:620px}
#chatView .cw-mark{width:44px;height:44px;border-radius:12px;background:var(--accent-soft);
  color:var(--accent-ink);box-shadow:none}
#chatView .cw-card{border-radius:8px}
#chatView .cw-card:hover{background:var(--accent-soft);border-color:var(--accent-line)}
#chatView #chatSend{background:var(--ink);border-color:var(--ink);color:var(--surface)}
#chatView #chatSend:hover{background:var(--ink);border-color:var(--ink);color:var(--surface);
  filter:brightness(.94)}

/* ---- Dialogs / overlays ------------------------------------------------ */
.mask{background:rgba(41,40,36,.34)}
.dlg{border:1px solid var(--line);border-radius:14px;
  box-shadow:0 24px 64px rgba(41,40,36,.16);padding:24px 26px}
.dlg h3{font-size:17px;font-weight:600;letter-spacing:-.01em}
.dlg .db{font-size:14px;line-height:1.75}
.dlg .dbar{margin-top:22px;gap:9px}
.mask.on .dlg{animation:tpDlgIn .18s cubic-bezier(.2,.7,.3,1) both}
@keyframes tpDlgIn{from{opacity:0;transform:scale(.985)}to{opacity:1;transform:none}}
.msel .msPanel,.morePanel,.batchPanel,.acctMenu{box-shadow:var(--shadow);
  border-color:var(--line);border-radius:10px}
.toast{border-radius:8px;box-shadow:var(--shadow);font-size:12.5px}
.toast.ok,.toast.danger,.toast.info{color:var(--surface)}
.rp-unit-del:hover,.morePanel .clearRow .btn:hover,.dupwarn .btn:hover{
  background:var(--bad);border-color:var(--bad);color:var(--surface)}
.richeditor td,.richeditor th{border-color:var(--line-2)}

/* ---- Login -------------------------------------------------------------- */
/* 登录页保留原始的三层背景（白蒙版 + 淡蓝光晕 + 大楼照片），
   只是尺寸/圆角沿用 v4.1 的组件形状。 */
.loginwrap{background:
  linear-gradient(135deg,rgba(255,255,255,.93) 0%,rgba(255,255,255,.78) 45%,
                  rgba(238,244,255,.72) 100%),
  radial-gradient(circle at 72% 28%,rgba(51,112,255,.10),transparent 58%),
  url("/login-bg.jpg") center 42% / cover no-repeat,
  linear-gradient(160deg,#eef2f8 0%,#e6ecf6 100%)}
.loginbox{background:var(--surface);border:1px solid var(--line);
  border-radius:14px;box-shadow:0 24px 64px rgba(24,32,48,.16);padding:32px 30px 26px}
.loginbrand{font-size:18px;font-weight:600}
.logindot{background:var(--accent);border-radius:2px}
.logininput{background:var(--surface-2);border-color:transparent}
.logininput:focus{background:var(--surface);border-color:var(--accent-line)}
.loginbtn{border-radius:8px;font-weight:600}

/* ---- Dark-mode fixes for legacy hard-coded colours --------------------- */
:root[data-theme="dark"] .loginwrap{
  background:linear-gradient(135deg,rgba(23,24,27,.95),rgba(31,32,36,.9)),
    url("/login-bg.jpg") center 42% / cover no-repeat,var(--ground)}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]) .loginwrap{
    background:linear-gradient(135deg,rgba(23,24,27,.95),rgba(31,32,36,.9)),
      url("/login-bg.jpg") center 42% / cover no-repeat,var(--ground)}
}
:root[data-theme="dark"] .toast{background:var(--surface-2);color:var(--ink)}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]) .toast{background:var(--surface-2);color:var(--ink)}
}
:root[data-theme="dark"] .pv,
:root[data-theme="dark"] .loginbox{background:var(--surface)}

/* ---- Responsive -------------------------------------------------------- */
@media (max-width:1280px){
  body{grid-template-columns:200px 1fr}
  .wrap{grid-template-columns:340px 1fr}
  .search{width:230px}
  .detail{padding:0 22px 40px}
}
@media (max-width:1100px){
  body{grid-template-columns:64px 1fr}
  .side{padding:12px 6px}
  .side button{justify-content:center;padding:0;box-shadow:none}
  .side button.on{box-shadow:none}
  .wrap{grid-template-columns:1fr}
  .detail{padding:0 18px 36px}
  .top{height:auto;min-height:52px;flex-wrap:wrap;gap:6px;padding:6px 14px}
}
@media (max-width:720px){
  .dlg{padding:20px}
  .dname{font-size:22px}
  .rp-head h2{font-size:22px}
}
@media (prefers-reduced-motion: reduce){
  .mask.on .dlg{animation:none!important}
  .btn,.row,.side button,.cw-card,.lgitem,.seg button{transition:none!important}
}

/* ============================================================================
   v5 — Apple-inspired visual system
   ----------------------------------------------------------------------------
   A final, purely visual layer on top of the existing business UI. It changes
   colour, type, spacing, hairlines, radii and motion only — every DOM contract,
   route, permission and data path stays exactly as it was.

   Design language
     · canvas #f5f5f7 / content white / ink #1d1d1f
     · one accent — Apple Blue #0071e3
     · hairlines and soft fills instead of heavy borders
     · 14–22px radii, pill controls, generous rhythm
     · SF-style type scale with tight display tracking
     · motion that answers an action: 200–420ms, Apple ease curves
   ============================================================================ */

/* ---------- 0. Elevation tokens ---------- */
:root{
  --elev:0 1px 2px rgba(0,0,0,.06),0 0 0 .5px rgba(0,0,0,.05);
  --elev-2:0 10px 28px rgba(0,0,0,.10);
  --elev-card:0 1px 2px rgba(0,0,0,.03),0 14px 40px rgba(0,0,0,.06);
}
:root[data-theme="dark"]{
  --elev:0 1px 3px rgba(0,0,0,.5),0 0 0 1px #313134;
  --elev-2:0 14px 36px rgba(0,0,0,.55);
  --elev-card:0 1px 2px rgba(0,0,0,.35),0 18px 46px rgba(0,0,0,.42);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --elev:0 1px 3px rgba(0,0,0,.5),0 0 0 1px #313134;
    --elev-2:0 14px 36px rgba(0,0,0,.55);
    --elev-card:0 1px 2px rgba(0,0,0,.35),0 18px 46px rgba(0,0,0,.42);
  }
}

/* ---------- 1. Global text, scrollbars, focus ---------- */
::selection{background:rgba(0,113,227,.18);color:var(--ink)}
*{scrollbar-color:var(--scroll-thumb) transparent;scrollbar-width:thin}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--scroll-thumb);border:3px solid transparent;
  background-clip:content-box;border-radius:999px}
::-webkit-scrollbar-thumb:hover{background:var(--ink-3);border:3px solid transparent;
  background-clip:content-box}
a{color:var(--accent-ink);text-decoration:none}
a:hover{text-decoration:underline}
h1,h2,h3,h4{letter-spacing:-.015em}
.num,.stat-n,.rh-v,.rp-card .v,.rh-ring-center b,.count b{letter-spacing:-.02em}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,
[tabindex]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* ---------- 2. Sidebar ---------- */
body{grid-template-columns:248px 1fr}
.side{padding:18px 14px 12px;background:var(--nav);border-right:1px solid var(--line)}
.sbrand{padding:4px 10px 20px;gap:11px;font-size:15.5px;letter-spacing:-.01em}
.sbrand img{width:30px;height:30px;border-radius:9px;
  box-shadow:0 1px 2px rgba(0,0,0,.14),0 0 0 .5px rgba(0,0,0,.06)}
.sbrand b{font-weight:600}
.sgroup{padding:16px 12px 8px;font-size:10.5px;font-weight:600;
  letter-spacing:.12em;color:var(--ink-3)}
.side button{height:38px;padding:0 11px;gap:11px;border-radius:10px;
  font-size:13.5px;color:var(--ink-2);
  transition:background-color .2s var(--ease),color .2s var(--ease),
    box-shadow .2s var(--ease),transform .12s var(--ease)}
.side button:hover{background:var(--fill-hover);color:var(--ink)}
.side button:active{transform:scale(.985)}
.side button.on{background:var(--surface);color:var(--ink);font-weight:600;
  box-shadow:var(--elev)}
.side button.on .ico{color:var(--accent);opacity:1;stroke-width:1.8}
.sfoot{padding:12px 2px 2px;border-top:1px solid var(--line)}
.acct{height:42px;padding:0 9px;border-radius:10px;
  transition:background-color .2s var(--ease)}
.acct:hover{background:var(--fill-hover)}
.acct .av{width:26px;height:26px;font-size:11px;font-weight:600;color:#fff;
  background:var(--accent);box-shadow:none}
.acct .txt b{font-size:13px}
.acct .txt i{font-size:11px}
.acctMenu{border:1px solid var(--line);border-radius:14px;background:var(--surface);
  box-shadow:var(--elev-2);padding:6px}
.acctMenu button{height:34px;line-height:34px;border-radius:8px;font-size:13px}
.acctMenu button:hover{background:var(--fill-hover)}
.acctMenu .ver{font-size:10.5px}

/* ---------- 3. Top bars / filters ---------- */
.top{height:60px;padding:0 24px;gap:10px;background:var(--surface);
  border-bottom:1px solid var(--line)}
.brand{font-size:14px;font-weight:600}
.search{width:300px;height:36px;padding:0 15px;border:1px solid transparent;
  border-radius:999px;background:var(--surface-2);color:var(--ink);font-size:13px;
  transition:background-color .2s var(--ease),border-color .2s var(--ease),
    box-shadow .2s var(--ease)}
.search::placeholder{color:var(--ink-3)}
.search:hover{border-color:var(--line)}
.search:focus{background:var(--surface);border-color:var(--accent);box-shadow:var(--ring)}
.sel,.msel > .msbtn,.vsel,.stagesel{height:36px;padding:0 13px;border:1px solid transparent;
  border-radius:999px;background:var(--surface-2);color:var(--ink-2);font-size:13px;
  transition:background-color .2s var(--ease),border-color .2s var(--ease),
    color .2s var(--ease),box-shadow .2s var(--ease)}
.sel:hover,.msel > .msbtn:hover,.vsel:hover,.stagesel:hover{
  border-color:var(--line);color:var(--ink)}
.sel:focus,.msel > .msbtn:focus,.vsel:focus,.stagesel:focus{
  border-color:var(--accent);box-shadow:var(--ring)}
.msel > .msbtn.active{background:var(--accent-soft);border-color:transparent;
  color:var(--accent-ink)}
.count{font-size:12px;color:var(--ink-3);letter-spacing:.01em}
.count b{font-size:15px;font-weight:600;color:var(--ink)}
.vdiv{height:16px;background:var(--line)}
.top2{padding:9px 20px;gap:6px;background:var(--surface);border-bottom:1px solid var(--line)}
.jobbar,.archbar{padding:9px 24px;background:var(--surface);border-color:var(--line)}
.tabbar{padding:8px 24px;background:var(--surface);border-bottom:1px solid var(--line)}
.tabs{display:flex;flex-wrap:wrap;gap:2px;padding:3px;border-radius:999px;
  background:var(--surface-2)}
.tabs button{padding:5px 14px;border:1px solid transparent;border-radius:999px;
  background:transparent;color:var(--ink-2);font-size:12.5px;
  transition:background-color .2s var(--ease),color .2s var(--ease),
    box-shadow .2s var(--ease)}
.tabs button:hover{background:var(--fill-hover);color:var(--ink)}
.tabs button.on{background:var(--surface);border-color:transparent;color:var(--ink);
  font-weight:600;box-shadow:var(--elev)}

/* ---------- 4. Buttons ---------- */
.btn{height:34px;padding:0 15px;border:1px solid transparent;border-radius:999px;
  background:var(--surface-2);color:var(--ink);font-size:13px;font-weight:500;
  line-height:1.2;transition:background-color .2s var(--ease),color .2s var(--ease),
    border-color .2s var(--ease),transform .12s var(--ease),box-shadow .2s var(--ease)}
.btn:hover{background:var(--line);border-color:transparent;color:var(--ink)}
.btn:active{transform:scale(.97)}
.btn.primary,.btn-primary{background:var(--accent-soft);border-color:transparent;
  color:var(--accent-ink);font-weight:600}
.btn.primary:hover,.btn-primary:hover{background:var(--accent-hi);color:var(--accent-ink)}
.btn-green{background:var(--ok-soft);border-color:transparent;color:var(--ok)}
.btn-green:hover{background:var(--ok-hi);border-color:transparent;color:var(--ok)}
.btn-ok{background:var(--ok);border-color:var(--ok);color:#fff;font-weight:600}
.btn-ok:hover{background:var(--ok);border-color:var(--ok);color:#fff;filter:brightness(1.06)}
.btn-danger,.btn.danger{background:var(--bad-soft);border-color:transparent;
  color:var(--bad);font-weight:500}
.btn-danger:hover,.btn.danger:hover{background:var(--bad-line);border-color:transparent;
  color:var(--bad)}
.btn.sm{height:28px;padding:0 11px;font-size:12px}
.btn-lg{height:36px;padding:0 16px;font-size:13.5px}
.top2 .btn-lg{height:32px;padding:0 11px;font-size:12px}
.btn:disabled{opacity:.38;cursor:not-allowed;transform:none}
.btn.busy{border-color:var(--accent-line);color:var(--accent-ink);
  background:var(--accent-soft)}
.btn.reviewed{background:var(--ok-soft);border-color:transparent;color:var(--ok)}
.btn.reviewed:hover{background:var(--ok-hi);border-color:transparent;color:var(--ok)}
#btnProp.on{background:var(--accent-soft);border-color:transparent;color:var(--accent-ink)}

/* iOS-style segmented control */
.seg{display:inline-flex;gap:2px;padding:3px;border:0;border-radius:999px;
  background:var(--surface-2);overflow:visible}
.seg button{border:0;border-right:0;border-radius:999px;background:transparent;
  color:var(--ink-2);padding:5px 14px;font-size:12.5px;
  transition:background-color .2s var(--ease),color .2s var(--ease),
    box-shadow .2s var(--ease)}
.seg button:hover{background:var(--fill-hover);color:var(--ink)}
.seg button.on{background:var(--surface);color:var(--ink);font-weight:600;
  box-shadow:var(--elev)}
.tier-sec .seg button{font-size:13.5px;padding:6px 18px}

/* ---------- 5. Inputs ---------- */
input:not([type=checkbox]):not([type=radio]),select,textarea{
  border:1px solid transparent;border-radius:10px;background:var(--surface-2);
  color:var(--ink);transition:background-color .2s var(--ease),
    border-color .2s var(--ease),box-shadow .2s var(--ease)}
input:not([type=checkbox]):not([type=radio]):hover,select:hover,textarea:hover{
  border-color:var(--line-2)}
input:not([type=checkbox]):not([type=radio]):focus,select:focus,textarea:focus{
  outline:none;background:var(--surface);border-color:var(--accent);
  box-shadow:var(--ring)}
input[type=checkbox],input[type=radio]{accent-color:var(--accent)}

/* ---------- 6. Candidate / job list ---------- */
.wrap{grid-template-columns:380px 1fr}
.listwrap{background:var(--surface);border-right:1px solid var(--line)}
.list{padding:8px}
.row{grid-template-columns:3px 18px 30px 1fr auto;gap:10px;
  padding:10px 12px 10px 0;margin:2px 0;border-bottom:0;border-radius:12px;
  transition:background-color .18s var(--ease)}
.row.jrow{grid-template-columns:3px 1fr auto}
.row:hover{background:var(--surface-2)}
.row.on{background:var(--accent-soft)}
.rowbar{width:3px;min-height:38px;border-radius:999px}
.row.on .rowbar{background:var(--accent)}
.row .nm{font-size:14.5px;font-weight:600;letter-spacing:-.01em}
.row .sub{font-size:12.5px;color:var(--ink-3)}
.av{width:30px;height:30px;background:var(--surface-2);font-size:11.5px}
.pager{padding:10px 14px;background:var(--surface);border-top:1px solid var(--line);
  font-size:12px;color:var(--ink-3);gap:6px 10px}
.pager .btn{height:28px;padding:0 11px;font-size:12px}
.pgjumpwrap{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.pgbreak{flex-basis:100%;width:0;height:0;margin:0;padding:0}
.pgwrap .pg,.ppager .pg,.pgjump{border-radius:8px;border-color:var(--line);
  background:var(--surface)}
.tier{border-radius:999px;font-size:11.5px;font-weight:600}
.empty{padding:36px 18px;color:var(--ink-3);font-size:13px}
.listmask{background:rgba(0,0,0,.22)}

/* ---------- 7. Detail ---------- */
.detail{padding:0 36px 56px;background:var(--surface)}
.detail > *{max-width:1180px}
.detail-toolbar{padding:16px 0 12px;background:var(--surface);
  border-bottom:1px solid var(--line);box-shadow:none}
.detail.swap > *:not(.detail-toolbar){animation:appleViewIn .32s var(--ease-out) both}
.profile-head{margin-top:24px}
.dname{font-size:28px;font-weight:650;letter-spacing:-.03em;line-height:1.22}
.av-lg{width:104px;height:104px;border:0;border-radius:18px;background:var(--surface-2);
  box-shadow:0 1px 3px rgba(0,0,0,.08);font-size:32px}
.dmeta{font-size:14px;gap:8px 12px;color:var(--ink-2)}
#detail .dmeta .chipl{font-size:14px}
.dmeta .edv{background:var(--surface-2);border:1px solid transparent;border-radius:999px;
  padding:4px 12px;font-size:13.5px}
.dmeta .edv:hover{background:var(--accent-soft);border-color:transparent;
  color:var(--accent-ink)}
.dmeta .edv.empty{color:var(--ink-3)}
.dmeta.dmeta2{margin-top:12px}
#detail .dmeta.dmeta2 .chipl{font-size:13px;font-weight:600;padding:4px 12px;
  border:1px solid transparent;border-radius:999px;background:var(--surface-2);
  color:var(--ink-2)}
.dmeta.dmeta2 .stagechip{background:var(--accent-soft);border-color:transparent;
  color:var(--accent-ink)}
.dmeta.dmeta2 .offerchip{background:var(--ok-soft);border-color:transparent;color:var(--ok)}
.dmeta.dmeta2 .ochip{font-size:13px;padding:4px 12px}
.fdsep{background:var(--line-2)}
.vsep{background:var(--line)}

/* sections */
.sec{margin-top:30px}
.sec > h3,#detail .sec > h3{font-size:17px;font-weight:600;letter-spacing:-.015em;
  color:var(--ink);border-bottom:1px solid var(--line);padding-bottom:10px;
  margin-bottom:14px}
.sec.tier-sec{margin-top:24px}
.sec.tier-sec > h3{font-size:17px;border-bottom:0;padding-bottom:0;margin-bottom:10px}
.profile-layout{gap:0 32px;font-size:14px}
.profile-layout .sec > h3{font-size:15px}
#detail .ev{font-size:14px;line-height:1.75;background:var(--surface-2);
  border-left:0;border-radius:12px;padding:12px 14px;color:var(--ink-2)}
#detail .sec .ev b{color:var(--ink);font-weight:600}
#detail .sum{font-size:15.5px;font-weight:500;line-height:1.7;color:var(--ink)}
#detail .sec.quiet > h3{font-size:14px;letter-spacing:0;color:var(--ink-2)}
#detail .sec.quiet .ev{font-size:13px;background:var(--surface-2)}
.honosec .ev{background:var(--surface-2);border-radius:12px;padding:10px 13px}
.proj-item{border-left:0;border-radius:14px;background:var(--surface-2);
  padding:12px 14px}
.proj-dir{border:0;border-radius:12px;background:var(--accent-soft)}
.proj-dir span{color:var(--accent-ink)}
.fgroup{background:var(--surface-2);border-radius:14px;padding:14px 16px}
.fgroup > h4{font-size:12.5px;font-weight:600;color:var(--ink-3);letter-spacing:.02em}
.ivqwrap{background:var(--surface-2);border-radius:14px;padding:14px 16px}
.ivrow + .ivrow{border-top:1px solid var(--line)}
.ivrow .multi{border-color:transparent;background:var(--surface-2);border-radius:10px}
.edu-band{border-color:var(--line)}
.edu-edit{border-top-color:var(--line)}
.fld > .fl,.lbl2{color:var(--ink-3)}

/* chips */
.chipl,.jchip,.rk,.stg,.ochip,.achip,.jst,.prisk,.flagbad,.flagwarn,.flagneutral,
.mchip,.tochip,.ivrow .mchip{border-radius:999px;border-color:transparent}
.chipl{padding:3px 10px;font-size:12px;font-weight:500;background:var(--accent-soft);
  color:var(--accent-ink)}
.chipl.gray{background:var(--surface-2);color:var(--ink-2)}
.jchip{padding:2px 10px;font-size:12px;background:var(--surface-2);color:var(--ink-2)}
.jchip:hover{background:var(--accent-soft);color:var(--accent-ink);border-color:transparent}
.rk{padding:2px 9px;font-size:12px;background:var(--accent-soft);color:var(--accent-ink)}
.rk.none{background:transparent;color:var(--ink-3)}
.stg,.jst{padding:3px 9px;font-size:11.5px}
.ochip{padding:3px 11px;font-size:12.5px}
.achip{padding:2px 9px}
.prisk{padding:2px 9px}
.skill span{border:1px solid transparent;border-radius:999px;background:var(--surface-2);
  padding:4px 11px;font-size:12.5px;color:var(--ink-2)}
.skill .hit{background:var(--ok-soft);color:var(--ok);border-color:transparent}
.skill .miss{background:var(--bad-soft);color:var(--bad);border-color:transparent}
.skill .plain.unverified{background:transparent;border-style:dashed;border-color:var(--line-2)}

/* banners / callouts */
.srcbar{margin-top:16px;padding:12px 16px;background:var(--ok-soft);border:0;
  border-radius:14px}
.srcbar .btn{border-radius:999px;background:var(--surface);border-color:transparent}
.srcbar .btn:hover{background:var(--ok);border-color:var(--ok);color:#fff}
.dupwarn{border:0;border-radius:14px;background:var(--warn-soft);padding:12px 14px}
.dupwarn .btn{border-radius:999px;border-color:transparent;background:var(--surface)}
.quality{border-left:3px solid var(--warn);border-radius:12px;padding:12px 14px}
.sensline{border:0;border-radius:12px;padding:10px 13px}
.sensline.none{background:var(--surface-2)}
.dcard{border:0;border-radius:12px;background:var(--accent-soft);padding:12px 14px}
.dcard.prop{background:var(--warn-soft)}
.msgline{border:0;border-radius:999px;background:var(--ok-soft);color:var(--ok)}
.msgline.bad{background:var(--bad-soft);color:var(--bad)}
.msgline.busy{background:var(--surface-2);color:var(--ink-3)}
.taskbar{background:var(--warn-soft);border-bottom:1px solid var(--line)}
.progfill{background:var(--accent);border-radius:999px}
/* 思考块：DeepSeek 式“已思考（用时 N 秒）”折叠头 + 左侧竖线正文。 */
.thinkbox{background:transparent;border:0;border-radius:0;margin:0 0 18px;
  overflow:visible}
.thinkbox > summary{display:flex;align-items:center;gap:6px;padding:3px 0;
  font-size:13px;color:var(--ink-3);cursor:pointer;list-style:none}
.thinkbox > summary::-webkit-details-marker{display:none}
.thinkbox > summary::before{content:'›';font-size:17px;line-height:1;
  color:var(--ink-3);transition:transform .15s var(--ease)}
.thinkbox[open] > summary::before{transform:rotate(90deg)}
.thinkbox .think-body{margin:8px 0 0;padding:2px 0 2px 16px;
  border-left:2px solid var(--line-2);border-radius:0;
  background:transparent;color:var(--ink-3);font-size:13px;
  line-height:1.75;max-height:210px;overflow:auto;white-space:normal}
.thinkbox .think-body p{margin:0 0 6px}
.thinkbox .think-body p:last-child{margin-bottom:0}
.think-live{display:block;margin:0 0 14px;padding:0;border:0;
  border-radius:0;background:transparent}
.think-live-h{display:flex;align-items:center;gap:6px;font-size:12.5px;
  color:var(--ink-3);margin-bottom:6px}
.think-live-h::before{content:'›';font-size:17px;line-height:1;
  color:var(--accent);transform:rotate(90deg)}
.think-live .think-body{margin:0;padding:2px 0 2px 16px;
  border-left:2px solid var(--line-2);border-radius:0;
  background:transparent;color:var(--ink-3);font-size:13px;
  line-height:1.75;max-height:180px;overflow:auto;white-space:pre-wrap}
.mchart{border:0;border-radius:14px;background:var(--surface-2)}
.pvbox{border:0;border-radius:12px;background:var(--surface-2)}
.outitem{border:0;border-radius:10px;background:var(--surface-2)}
.outitem:hover{background:var(--fill-hover)}
.metric-row{border-radius:10px;background:var(--surface-2)}
.rawbox{border:0;border-radius:14px;overflow:hidden;box-shadow:0 0 0 1px var(--line)}
.rawbox > summary{background:var(--surface-2);color:var(--ink-2)}
.auditbox{border:0;border-radius:14px;overflow:hidden;box-shadow:0 0 0 1px var(--line)}
.auditbox > summary{background:var(--surface-2);color:var(--ink-2);font-size:12.5px}
.auditbox > summary:hover{background:var(--line)}
.auditbox th{background:transparent;color:var(--ink-3);border-bottom:1px solid var(--line)}
.auditbox td{border-bottom:1px solid var(--line)}

/* fields that open on click */
.edv{border-radius:8px;background:var(--surface-2)}
.edv:hover{background:var(--accent-soft);border-color:transparent;color:var(--accent-ink)}
.edu-field input,.edu-field select{border-radius:9px;background:var(--surface-2)}

/* duplicate / pinned comparison tabs */
.pinned-tabs{margin:0 -36px;padding:8px 36px;background:var(--surface);
  border-bottom:1px solid var(--line);gap:6px}
.pinned-tab{border:0;border-radius:999px;background:var(--surface-2);
  padding:7px 12px;margin-bottom:0;color:var(--ink-2)}
.pinned-tab:hover{background:var(--fill-hover)}
.pinned-tab.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:600;
  border:0;box-shadow:none}
.pinned-tab.on .pt-name{color:var(--accent-ink)}
.pinned-profile{padding-top:22px}

/* ---------- 8. Overview / analysis ---------- */
.ovcol,.ov-analysis,.stat{background:var(--surface-2);border:0;border-radius:18px;
  padding:20px 22px;box-shadow:none}
.ovcol > h3{font-size:16px;font-weight:600;border-bottom:1px solid var(--line);
  padding-bottom:12px;margin-bottom:14px}
.ov-analysis{padding:20px 22px}
.ana-workbench{background:transparent;border:0;border-radius:0;padding:0}
.ana-controls{background:var(--surface);border:0;border-radius:14px;padding:14px 16px;
  box-shadow:0 0 0 1px var(--line)}
.ov-todos .todo{border-bottom:1px solid var(--line);padding:12px 10px;margin:0 -10px;
  border-radius:12px;cursor:pointer;
  transition:background-color .15s var(--ease),transform .12s var(--ease)}
.ov-todos .todo:hover{background:var(--surface)}
.ov-todos .todo:active{background:var(--fill-hover)}
.ov-todos .todo:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.ov-todos .todo::after{content:'›';margin-left:auto;align-self:center;flex:none;
  color:var(--ink-3);font-size:17px;line-height:1;
  transition:color .15s var(--ease),transform .15s var(--ease)}
.ov-todos .todo:hover::after{color:var(--accent);transform:translateX(2px)}
.todo-t{font-size:14px}
.todo-w{font-size:12.5px;color:var(--ink-2)}
.todo-a{font-size:12.5px}
.stat{padding:16px 18px}
.stat-n{font-size:30px;font-weight:600}
.stat-l{font-size:12.5px;color:var(--ink-2)}
.stat-h{font-size:11.5px;color:var(--ink-3)}
.ovjob-track{height:8px;background:var(--surface);border-radius:999px}
.ovjob-track i{background:var(--accent);border-radius:999px}
.ovjob-head{font-size:12px;color:var(--ink-2)}
.lgitem{border-radius:8px;font-size:12.5px}
.lgitem:hover{background:var(--fill-hover)}
.anack{border-radius:999px;border-color:transparent;background:var(--surface)}
.anack:hover{border-color:transparent;background:var(--fill-hover)}
.anack.on{background:var(--accent-soft);border-color:transparent;color:var(--accent-ink)}
.dimbtn{border-radius:999px;border-color:transparent;background:var(--surface)}
.dimbtn.on{background:var(--accent-soft);border-color:transparent;color:var(--accent-ink)}
.barchart .btrack{height:10px;border-radius:999px;background:var(--surface)}
.barchart .btrack i{border-radius:999px}

/* ---------- 9. Recruit progress ---------- */
#recruitView.on{padding:28px 32px 56px;background:var(--ground)}
.rp-head h2{font-size:28px;font-weight:650;letter-spacing:-.03em}
.rp-card,.rp-degree,.rp-health-card,.rp-wrap,.recruit-hero{background:var(--surface);
  border:0;border-radius:20px;box-shadow:0 0 0 1px var(--line)}
.rp-card{padding:22px 24px}
.rp-card::before{display:none}
.rp-card .k{font-size:12.5px;color:var(--ink-3);font-weight:500;letter-spacing:.01em}
.rp-card .v{font-size:36px;font-weight:650;letter-spacing:-.035em;margin-top:4px}
.rp-degree{padding:18px 20px}
.rp-degree .dt{font-size:15px;font-weight:600}
.recruit-hero{padding:28px 32px;border-radius:24px}
.recruit-hero::after{display:none}
.rh-ring-center b{font-size:40px;font-weight:650;letter-spacing:-.04em}
.rh-ring-center span{font-size:12px;color:var(--ink-3)}
.rh-kpis{border-bottom-color:var(--line);padding-bottom:18px;margin-bottom:18px}
.rh-v{font-size:30px;font-weight:650;letter-spacing:-.03em}
.rh-k{font-size:11.5px;color:var(--ink-3);letter-spacing:.01em}
.rh-degrees{border-top-color:var(--line);padding-top:16px}
.rh-degree{border-left-color:var(--line)}
.rh-dname{font-weight:600}
.rp-tabs{display:inline-flex;gap:2px;border:0;background:var(--surface-2);
  border-radius:999px;padding:3px;margin-bottom:18px}
.rp-tab{border:0;border-radius:999px;padding:7px 17px;font-size:13px;color:var(--ink-2);
  transition:background-color .2s var(--ease),color .2s var(--ease),
    box-shadow .2s var(--ease)}
.rp-tab:hover{background:var(--fill-hover);color:var(--ink)}
.rp-tab.on{background:var(--surface);color:var(--ink);border:0;font-weight:600;
  box-shadow:var(--elev)}
.rp-bar .btn,.rp-toolbar .btn{border-radius:999px}
.rp-wrap{border-radius:18px}
.rp-table th{background:var(--surface-2);color:var(--ink-3);font-size:11.5px;
  font-weight:600;letter-spacing:.03em;padding:12px 10px;border-bottom:1px solid var(--line)}
.rp-table th.plan{background:var(--surface-2);color:var(--warn)}
.rp-table th.calc{background:var(--surface-2);color:var(--accent-ink)}
.rp-table th.sign{background:var(--surface-2);color:var(--plum)}
.rp-table td{padding:12px 10px;border-bottom:1px solid var(--line)}
.rp-table tbody tr.unit-even td{background:var(--row-alt)}
.rp-table tbody tr.unit-even td.unit{background:var(--row-unit)}
.rp-table tbody tr.unit-even.has-sign td{background:var(--ok-soft)}
.rp-table tbody tr.unit-even.has-sign td.unit{background:var(--ok-hi)}
.rp-table input.rp-num{border-radius:8px;border-color:transparent;background:var(--surface-2)}
.rp-signee{background:var(--accent-soft);color:var(--accent-ink);border-radius:999px;
  font-size:11.5px;padding:3px 9px}
.rp-signee:hover{background:var(--accent-hi)}
.rp-empty{border:1px dashed var(--line-2);background:var(--surface);border-radius:18px}
.rp-health-card{padding:16px 18px}
.rp-form > input,.rp-date-range input{border-radius:10px;background:var(--surface-2);
  border-color:transparent;height:38px}

/* ---------- 10. Schedule ---------- */
#scheduleView.on{padding:28px 32px 22px;background:var(--ground)}
.sched-head{margin-bottom:16px}
.sched-title{font-size:28px;font-weight:650;letter-spacing:-.03em}
.sched-sub{font-size:12.5px;color:var(--ink-3)}
.sched-tools .btn{border-radius:999px}
.sched-month,.sched-week{border:0;border-radius:18px;background:var(--surface);
  box-shadow:0 0 0 1px var(--line);overflow:hidden}
.sched-dow{background:var(--surface-2);color:var(--ink-3);font-size:11px;
  font-weight:600;letter-spacing:.06em;border-color:var(--line);padding:10px 11px}
.sched-cell{min-height:124px;border-color:var(--line);padding:7px 8px}
.sched-cell.out{background:var(--surface-2)}
.sched-cell.out .sched-daynum{color:var(--ink-3);opacity:.5}
.sched-daynum{min-width:24px;height:24px;font-size:12.5px;font-weight:600}
.sched-cell.today .sched-daynum{background:var(--accent);color:#fff;
  box-shadow:0 2px 8px rgba(0,113,227,.35)}
.sched-ev{border:0;border-left:0;border-radius:7px;background:var(--accent-soft);
  color:var(--accent-ink);padding:3px 7px;font-weight:500;
  transition:background-color .15s var(--ease),box-shadow .15s var(--ease)}
.sched-ev:hover{background:var(--accent-hi);filter:none;box-shadow:none}
.sched-ev .t{color:var(--accent-ink)}
.sched-ev.dragging{opacity:.4}
.sched-cell.drag-over{background:var(--accent-soft);outline:2px dashed var(--accent);
  outline-offset:-3px}
.sched-weekhead{background:var(--surface-2);border-bottom:1px solid var(--line)}
.sched-weekhead .sched-dayhead{border-left-color:var(--line);color:var(--ink-2)}
.sched-weekhead .sched-dayhead.today{background:var(--accent-soft);color:var(--accent-ink)}
.sched-daycol,.sched-timecol{border-color:var(--line)}
.sched-hline{border-top-color:var(--line)}
.sched-hline:nth-child(2n){background:var(--fill-hover)}

/* ---------- 11. Mail / editor ---------- */
#mailView.on{background:var(--ground)}
.mform{padding:24px 28px 40px;background:var(--surface-2);
  border-right:1px solid var(--line)}
.medit{padding:24px 30px 0;background:var(--surface)}
.mform .sec > h3{font-size:15px}
.mrow > label{font-size:12.5px;color:var(--ink-3)}
.mrow input,.mrow select,.mrow > div > input,.mrow > div > select{
  height:38px;border-radius:10px;border-color:transparent;background:var(--surface);
  font-size:13.5px}
.mrow input:focus,.mrow select:focus,.mrow > div > input:focus,
.mrow > div > select:focus{background:var(--surface);border-color:var(--accent);
  box-shadow:var(--ring)}
.mtool button{border:0;border-radius:8px;background:var(--surface-2);color:var(--ink-2);
  height:28px;min-width:30px}
.mtool button:hover{background:var(--line);border-color:transparent;color:var(--ink)}
.mtool .btn-primary{background:var(--accent-soft);color:var(--accent-ink)}
.richeditor,#mBody{border:1px solid var(--line);border-radius:14px;background:var(--surface);
  padding:16px 18px;line-height:1.75}
.richeditor:focus,#mBody:focus{border-color:var(--accent);box-shadow:var(--ring)}
.medit-actions{border-top:1px solid var(--line);background:var(--surface);padding:14px 0 18px}
.tochip{font-size:14px;padding:5px 13px;border-radius:999px}
.pv{border:0;border-radius:16px;box-shadow:0 0 0 1px var(--line)}
.miss{border-radius:10px;background:var(--bad-soft);color:var(--bad)}
.acList{border:0;border-radius:14px;box-shadow:var(--elev-2)}
.acItem{border-radius:8px;padding:8px 11px}
.acItem:hover{background:var(--fill-hover);color:var(--ink)}
.acItem.sel{background:var(--accent-soft);color:var(--accent-ink)}

/* ---------- 12. Job management / archive ---------- */
#orgView.on{grid-template-columns:340px 1fr;background:var(--surface)}
.orgleft{background:var(--surface);border-right:1px solid var(--line)}
.orgleft .jobbar{border-top:1px solid var(--line);background:var(--surface)}
#archView.on{grid-template-columns:400px 1fr;background:var(--surface)}
#archBar{background:var(--surface);border-bottom:1px solid var(--line)}
.jrowacts .btn{height:30px;padding:0 12px;font-size:12px}
/* 岗位 JD：用满详情区宽度，短字段三列、长字段整行，标签在上方。 */
.jdgroup{background:var(--surface-2);border-radius:16px;padding:16px 18px;margin-top:10px}
.jdgrid{display:grid;grid-template-columns:1fr;gap:16px}
.jdgrid-3{grid-template-columns:minmax(0,1.8fr) minmax(0,1fr) minmax(0,1fr)}
.jdfield{display:flex;flex-direction:column;gap:7px;min-width:0;
  font-size:12px;font-weight:500;color:var(--ink-3);letter-spacing:.01em}
.jdgroup .jdfield input,.jdgroup .jdfield select,.jdgroup .jdfield textarea{
  width:100%;min-width:0;height:40px;padding:0 12px;
  border:1px solid transparent;border-radius:10px;
  background:var(--surface);color:var(--ink);font:inherit;font-size:14px;
  outline:none;transition:background-color .2s var(--ease),border-color .2s var(--ease),
    box-shadow .2s var(--ease)}
.jdgroup .jdfield textarea{height:auto;min-height:96px;padding:10px 12px;
  line-height:1.7;resize:vertical}
.jdgroup .jdfield .jdlist{min-height:64px}
.jdgroup .jdfield input:focus,.jdgroup .jdfield select:focus,
.jdgroup .jdfield textarea:focus{background:var(--surface);
  border-color:var(--accent);box-shadow:var(--ring)}
.jdgroup .jdfield .jdinline{display:flex;align-items:center;gap:8px;width:100%;
  height:40px;padding:0 12px;border:1px solid transparent;border-radius:10px;
  background:var(--surface)}
.jdgroup .jdfield .jdinline input{flex:1;width:auto;min-width:0;height:100%;
  padding:0;border:0;background:transparent;box-shadow:none}
.jdgroup .jdfield .jdinline input:focus{border:0;background:transparent;
  box-shadow:none}
.jdgroup .jdfield .jdinline:focus-within{border-color:var(--accent);
  box-shadow:var(--ring)}
.jdinline i{font-style:normal;font-size:13px;color:var(--ink-3);white-space:nowrap;flex:none}
.jdseg{display:flex;flex-wrap:wrap;gap:4px;padding:4px;border-radius:12px;
  background:var(--surface)}
.jdseg-btn{border:0;border-radius:8px;background:transparent;padding:6px 10px;
  font:inherit;font-size:12.5px;color:var(--ink-2);cursor:pointer;
  transition:background-color .15s var(--ease),color .15s var(--ease)}
.jdseg-btn:hover{background:var(--fill-hover);color:var(--ink)}
.jdseg-btn.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
@media (max-width:900px){.jdgrid-3{grid-template-columns:1fr}}
/* 插入表格弹窗：两列数字 + 右对齐动作，不再用会被 pre-wrap 拉散的行内布局。 */
.tblform{display:grid;grid-template-columns:1fr 1fr;gap:14px;white-space:normal}
/* 插入表格：贴近工具栏的小型行列选择器，不再占一个 560px 大弹窗。
   10×10 网格足够覆盖常用模板；更大的表插完用「＋行/＋列」扩展。 */
.tablepicker{position:fixed;z-index:320;width:232px;padding:11px 12px 10px;
  background:var(--surface);border:1px solid var(--line);border-radius:13px;
  box-shadow:0 18px 48px rgba(0,0,0,.22);user-select:none}
.tablepicker .tpgrid{display:grid;grid-template-columns:repeat(10,18px);gap:3px}
.tablepicker .tphint{white-space:normal}
.tablepicker .tplabel{white-space:nowrap}
.tablepicker .tpc{width:18px;height:18px;border:1px solid var(--line-2);border-radius:3px;
  background:var(--surface-2);cursor:pointer;transition:background-color .08s ease,border-color .08s ease}
.tablepicker .tpc.on{background:var(--accent-soft);border-color:var(--accent)}
.tablepicker .tpc.anchor{background:var(--accent);border-color:var(--accent)}
.tablepicker .tplabel{font-size:12.5px;font-weight:600;color:var(--ink);margin-bottom:8px;
  min-width:88px}
.tablepicker .tphint{font-size:10.5px;color:var(--ink-3);margin-top:8px;line-height:1.4}
.tblfield{display:flex;flex-direction:column;gap:7px;font-size:12.5px;
  color:var(--ink-3)}
.tblfield input{height:40px;padding:0 12px;border:1px solid transparent;
  border-radius:10px;background:var(--surface-2);font:inherit;font-size:14px;
  color:var(--ink);outline:none}
.tblfield input:focus{background:var(--surface);border-color:var(--accent);
  box-shadow:var(--ring)}
.tblbar{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
.tblhint{margin-top:16px;font-size:12.5px;line-height:1.75;color:var(--ink-3);
  white-space:normal}
@media (max-width:720px){.tblform{grid-template-columns:1fr}}

/* ---------- 13. System settings / users ---------- */
#sysView.on{padding:28px 32px 56px;background:var(--ground)}
#sysView > *{max-width:1080px}
.cfgsec{background:var(--surface);border-radius:18px;padding:20px 22px;
  box-shadow:0 0 0 1px var(--line)}
.cfgsec > h3{border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:14px}
.cfgsec .btn.sm{border-radius:999px}
.crow > label{font-size:12.5px}
.crow input,.crow select,.crow textarea,.cinput input,.cinput select,.cinput textarea,
.cval .ced{border-radius:10px;border-color:transparent;background:var(--surface-2);
  height:38px}
.cval textarea.ced,.crow textarea,.cinput textarea{height:auto}
.crow input:focus,.crow select:focus,.crow textarea:focus,.cinput input:focus,
.cinput select:focus,.cinput textarea:focus,.cval .ced:focus{
  border-color:var(--accent);box-shadow:var(--ring)}
.cedit{background:var(--surface-2);border-radius:14px;padding:12px 14px}
.dict-add input,.dict-bulk textarea{border-radius:10px;background:var(--surface-2);
  border-color:transparent}
#usersView{background:var(--ground)}
.uwrap{max-width:980px;margin:28px 32px 48px;padding:24px 26px 32px;background:var(--surface);
  border-radius:20px;box-shadow:0 0 0 1px var(--line)}
.uhead h3{font-size:20px;font-weight:600;letter-spacing:-.02em}
.utable th{border-bottom:1px solid var(--line);color:var(--ink-3);font-size:11.5px;
  letter-spacing:.04em}
.utable td{border-bottom:1px solid var(--line);padding:12px 10px}
.utable tbody tr:hover td{background:var(--fill-hover)}

/* ---------- 14. Pipeline / proposals ---------- */
.pipe{padding:10px 24px;background:var(--surface);border-bottom:1px solid var(--line)}
.pipehead{border-bottom:1px solid var(--line)}
.pcol{border-right:1px solid var(--line)}
.pchip{border-radius:8px;padding:4px 8px}
.pchip:hover{background:var(--accent-soft);color:var(--accent-ink)}
.propbox{background:var(--surface);border-bottom:1px solid var(--line);padding:12px 24px}
/* 待确认提案属于人才库视图：切到其它页面时必须一起让位，
   否则展开过一次之后会一直挂在每个页面底部（已修）。 */
body.v-mail .propbox,body.v-org .propbox,body.v-archive .propbox,
body.v-sys .propbox,body.v-users .propbox,body.v-chat .propbox,
body.v-recruit .propbox,body.v-schedule .propbox{display:none!important}
.prow{border-bottom:1px solid var(--line)}
.prisk{border-radius:999px;background:var(--surface-2)}

/* ---------- 15. Tables ---------- */
.mdtable{border-collapse:separate;border-spacing:0;font-size:13px;
  border-radius:12px;overflow:hidden;box-shadow:0 0 0 1px var(--line)}
.mdtable th,.mdtable td{border:0;border-bottom:1px solid var(--line);padding:9px 12px}
.mdtable th{background:var(--surface-2);color:var(--ink-3);font-size:11.5px;
  font-weight:600;letter-spacing:.03em}
.mdtable tr:last-child td{border-bottom:0}
.mdtable tbody tr:nth-child(even){background:transparent}
.mdtable tbody tr:hover{background:var(--fill-hover)}
.mdt-btn{border-radius:999px;border-color:transparent;background:var(--surface-2)}

/* ---------- 16. Chat ---------- */
#chatView.on{grid-template-columns:260px minmax(0,1fr) 320px;background:var(--surface)}
.chatconv{background:var(--nav);border-right:1px solid var(--line)}
.chatconv-head{padding:20px 16px 12px}
.chatconv-head h3{font-size:13px;font-weight:600;color:var(--ink-3);letter-spacing:.02em}
.chatconv-list{padding:0 10px 16px}
.chatconv-item{border-radius:10px;padding:11px 32px 11px 11px;margin-bottom:2px;
  transition:background-color .18s var(--ease)}
.chatconv-item:hover{background:var(--fill-hover)}
.chatconv-item.on{background:var(--surface);box-shadow:var(--elev);color:var(--ink)}
.chatconv-item.on .cc-title{color:var(--ink);font-weight:600}
.cc-title{font-size:13px}
.cc-meta{font-size:11px;color:var(--ink-3)}
.chatlog{padding:28px 32px}
/* LLM 回复不套气泡、不限制宽度：像 Claude Code 一样直接铺满正文栏。 */
.chatlog > *{max-width:none}
.msg{border-radius:18px;padding:12px 17px;font-size:15px;line-height:1.72;
  border:1px solid transparent}
.msg.assistant{background:transparent;border-color:transparent;
  border-radius:0;box-shadow:none;padding:0 0 2px;margin-bottom:20px;
  width:100%;max-width:none}
.msg.user{background:var(--accent-soft);border-color:transparent;color:var(--ink);
  border-bottom-right-radius:6px;max-width:min(100%,760px)}
.msg-actions{display:flex;align-items:center;gap:6px;margin-top:8px;
  opacity:.55;transition:opacity .18s var(--ease)}
.msg:hover .msg-actions,.msg:focus-within .msg-actions{opacity:1}
.msg.user .msg-actions{justify-content:flex-end}
.msg.assistant .msg-actions{justify-content:flex-start}
.msg-actions .mdt-btn{font-size:11.5px;padding:2px 9px}
.msg.user .msg-edit{width:min(560px,70vw);min-height:64px;padding:10px 12px;
  border:1px solid var(--accent);border-radius:14px;background:var(--surface);
  color:var(--ink);font:inherit;font-size:14.5px;line-height:1.65;resize:vertical;
  outline:none;box-shadow:var(--ring)}
.msg.user .msg-edit-actions{justify-content:flex-end}
#chatView .msg h3{font-size:17px;font-weight:600;letter-spacing:-.015em}
#chatView .msg h4{font-size:15.5px;font-weight:600}
#chatView .msg code{background:var(--surface);border-radius:6px}
#chatView .msg pre{background:var(--surface);border-radius:12px;padding:12px 14px}
#chatView .msg blockquote{border-left:3px solid var(--line-2)}
.chatwelcome{max-width:780px}
#chatView .chatwelcome h2{font-size:32px;font-weight:700;letter-spacing:-.035em;
  line-height:1.18;margin-bottom:10px}
#chatView .chatwelcome p{font-size:14px;line-height:1.7;color:var(--ink-3)}
#chatView .cw-mark{display:none}
#chatView .cw-asks{gap:10px}
/* AI 产出标记保持克制：灰底小标签，不用强调色。 */
.projectsec .ai-tag{background:var(--surface-2);border-color:transparent;
  color:var(--ink-3);font-weight:500;font-size:11px;padding:1px 8px}
#chatView .cw-card{background:var(--surface-2);border:0;border-radius:16px;
  padding:15px 17px;transition:background-color .22s var(--ease),
    box-shadow .22s var(--ease),transform .22s var(--ease)}
#chatView .cw-card:hover{background:var(--surface);border-color:transparent;
  transform:translateY(-2px);box-shadow:var(--elev-card)}
#chatView .cw-card .cw-q{font-size:13.5px;line-height:1.55}
.cw-w{font-size:11.5px;color:var(--ink-3)}
.cw-config .btn{background:transparent;color:var(--ink-3)}
.cw-config .btn:hover{background:var(--fill-hover);color:var(--ink)}
.chatinput{background:var(--surface);border-top:1px solid var(--line);
  padding:14px 32px 22px}
.chatinput textarea{border:1px solid transparent;border-radius:20px;
  background:var(--surface-2);padding:14px 64px 16px 16px;font-size:14.5px;
  line-height:1.7;transition:background-color .2s var(--ease),
    border-color .2s var(--ease),box-shadow .2s var(--ease)}
.chatinput textarea:focus{background:var(--surface);border-color:var(--accent);
  box-shadow:var(--ring)}
/* 发送 / 停止是**同一个圆钮**：空闲时是回车图标，生成中变成停止方块。
   一个按钮只表达一件事，不并排摆两个动作。 */
#chatSend,#chatSend.busy{position:absolute;right:10px;bottom:10px;
  width:40px;height:40px;min-width:40px;min-height:40px;padding:0;
  border-radius:50%;display:grid;place-items:center;opacity:1;pointer-events:auto;
  background:var(--ink);border:1px solid var(--ink);color:var(--surface);
  transition:background-color .2s var(--ease),transform .12s var(--ease),
    opacity .2s var(--ease)}
#chatSend .ci-ico{width:19px;height:19px;flex:none;fill:none;stroke:currentColor;
  stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
#chatSend .ci-stop{display:none}
#chatSend.busy .ci-enter{display:none}
#chatSend.busy .ci-stop{display:block}
#chatView #chatSend{background:var(--ink);border-color:var(--ink);color:var(--surface)}
#chatView #chatSend:hover{background:var(--ink);border-color:var(--ink);
  color:var(--surface);filter:brightness(1.12)}
#chatSend:active{transform:scale(.94)}
#chatSend:disabled{opacity:.4}
.cc-live{color:var(--accent-ink);font-weight:500}
.cc-live::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--accent);margin-right:5px;vertical-align:1px}
.chatside{background:var(--nav);border-left:1px solid var(--line);padding:20px}
.chatside .sec{margin-top:24px}
.chatside .sec > h3{font-size:14px;font-weight:600;letter-spacing:0;color:var(--ink-3);
  border-bottom:0;padding-bottom:0;margin:0 0 9px}
.chatside .sec > h3 .hint{font-size:11px;font-weight:400}
.ctxbar{height:9px;border-radius:999px;background:var(--surface-2)}
.ctxrow{font-size:12px}
.ask{border:0;border-radius:999px;background:var(--surface-2);color:var(--ink-2);
  font-size:12.5px;padding:6px 14px}
.ask:hover{background:var(--fill-hover);border-color:transparent;color:var(--ink)}
/* The assistant page now uses the same Apple Blue accent as the rest of the app. */
#chatView .chatwrap{--accent:#0071e3;--accent-ink:#0066cc;--accent-soft:#eaf4ff;
  --accent-line:#cfe4fb;--accent-hi:#dcecfd}
:root[data-theme="dark"] #chatView .chatwrap{--accent:#2997ff;--accent-ink:#7cbfff;
  --accent-soft:#0f2a44;--accent-line:#1f4668;--accent-hi:#163a5c}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]) #chatView .chatwrap{--accent:#2997ff;
    --accent-ink:#7cbfff;--accent-soft:#0f2a44;--accent-line:#1f4668;--accent-hi:#163a5c}
}
:root[data-theme="dark"] .msg.user{background:var(--accent-soft);border-color:transparent}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]) .msg.user{background:var(--accent-soft);
    border-color:transparent}
}

/* ---------- 17. Dialogs / popovers / toast ---------- */
.mask{background:rgba(0,0,0,.32)}
.dlg{background:var(--surface);border:0;border-radius:22px;padding:26px 28px;
  box-shadow:0 30px 90px rgba(0,0,0,.28)}
.dlg h3{font-size:20px;font-weight:600;letter-spacing:-.02em;margin-bottom:14px}
.dlg .db{font-size:14px;line-height:1.72;color:var(--ink-2)}
.dlg .dbar{margin-top:24px;gap:10px}
.dlg .dbar .btn{height:36px;padding:0 18px}
/* 弹窗内的自绘选项列表：和胶囊输入框同一套圆角、底色与强调色。 */
.picklist{display:flex;flex-direction:column;gap:2px;max-height:340px;overflow-y:auto;
  padding:6px;border:1px solid var(--line);border-radius:16px;background:var(--surface)}
.pickrow{display:flex;align-items:center;gap:12px;width:100%;padding:12px 14px;
  border:0;border-radius:10px;background:transparent;text-align:left;font:inherit;
  font-size:14px;color:var(--ink);cursor:pointer;
  transition:background-color .15s var(--ease),color .15s var(--ease)}
.pickrow:hover{background:var(--fill-hover)}
.pickrow.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
.pickrow.placeholder{color:var(--ink-3)}
.pickrow .pickcheck{margin-left:auto;opacity:0;color:var(--accent-ink);font-weight:700}
.pickrow.on .pickcheck{opacity:1}
/* 全站单选下拉的自绘弹层：与输入框同一套圆角、底色、强调色。
   原生 <select> 仍然保留（表单值与 change 事件不变），只替换弹层外观。 */
.selMenu{position:fixed;z-index:300;display:flex;flex-direction:column;gap:2px;
  padding:6px;background:var(--surface);border:1px solid var(--line);
  border-radius:14px;box-shadow:var(--elev-2);overflow-y:auto;min-width:160px;
  max-width:360px;outline:none}
.selOpt{display:flex;align-items:center;gap:10px;width:100%;padding:9px 11px;
  border:0;border-radius:9px;background:transparent;text-align:left;font:inherit;
  font-size:13px;color:var(--ink);cursor:pointer;white-space:nowrap;
  transition:background-color .14s var(--ease),color .14s var(--ease)}
.selOpt > span:first-child{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.selOpt:hover{background:var(--fill-hover)}
.selOpt.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
.selOpt .selChk{opacity:0;color:var(--accent-ink);font-weight:700}
.selOpt.on .selChk{opacity:1}
.selOpt.placeholder{color:var(--ink-3)}
.selOpt.placeholder.on{color:var(--accent-ink)}
.dlg-x{border-radius:999px}
.mask.on .dlg{animation:appleDlgIn .3s var(--ease-out) both}
.msel .msPanel,.morePanel,.acctMenu,.acList,.batchPanel{background:var(--surface);
  border:0;border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.18),
    0 0 0 1px rgba(0,0,0,.04);padding:8px}
.msItem{border-radius:9px;padding:7px 10px;font-size:12.5px}
.msItem:hover{background:var(--fill-hover)}
.msItem input{accent-color:var(--accent)}
.morePanel .clearRow{border-top:1px solid var(--line);padding-top:10px}
#batchPanel .btn,#batchOpsPanel .btn{border-radius:8px;background:transparent}
#batchPanel .btn:hover,#batchOpsPanel .btn:hover{background:var(--accent-soft);
  color:var(--accent-ink)}
.toast{left:50%;bottom:30px;padding:11px 20px;border-radius:999px;font-size:13px;
  background:rgba(29,29,31,.94);color:#fff;box-shadow:0 12px 32px rgba(0,0,0,.28);
  opacity:0;transform:translate(-50%,8px) scale(.98);
  transition:opacity .24s var(--ease),transform .24s var(--ease)}
.toast.on{opacity:1;transform:translate(-50%,0) scale(1)}
.toast.ok{background:rgba(29,127,67,.96);color:#fff}
.toast.warn{background:rgba(160,90,0,.96);color:#fff;border:0}
.toast.danger{background:rgba(215,0,21,.96);color:#fff}
.toast.info{background:rgba(29,29,31,.94);color:#fff}
:root[data-theme="dark"] .toast{background:rgba(44,44,46,.96);color:var(--ink)}
:root[data-theme="dark"] .toast.ok{background:rgba(29,127,67,.96);color:#fff}
:root[data-theme="dark"] .toast.danger{background:rgba(215,0,21,.96);color:#fff}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]) .toast{background:rgba(44,44,46,.96);color:var(--ink)}
  :root:not([data-theme="light"]) .toast.ok{background:rgba(29,127,67,.96);color:#fff}
  :root:not([data-theme="light"]) .toast.danger{background:rgba(215,0,21,.96);color:#fff}
}
.loadbar{height:3px}
.loadbar::before{background:var(--accent);border-radius:999px}

/* ---------- 18. Login ---------- */
.loginbox{width:min(380px,calc(100vw - 48px));padding:40px 36px 32px;
  background:rgba(255,255,255,.9);border:1px solid rgba(255,255,255,.75);
  border-radius:26px;box-shadow:0 40px 100px rgba(0,0,0,.22),
    inset 0 1px 0 rgba(255,255,255,.85)}
.loginbrand{font-size:20px;font-weight:600;letter-spacing:-.02em}
.logindot{width:10px;height:10px;border-radius:3px;background:var(--accent);
  box-shadow:none}
.loginlabel{font-size:12px;font-weight:500;color:var(--ink-3);margin-bottom:6px;
  letter-spacing:.01em}
.logininput{height:44px;padding:0 14px;margin-bottom:16px;border-radius:12px;
  background:rgba(0,0,0,.045);border:1px solid transparent;font-size:14px}
.logininput:focus{background:var(--surface);border-color:var(--accent);
  box-shadow:var(--ring)}
.loginbtn{height:46px;border-radius:999px;font-size:15px;font-weight:600;
  background:var(--accent-soft);border-color:transparent;color:var(--accent-ink);
  transition:background-color .2s var(--ease),transform .12s var(--ease)}
.loginbtn:hover{background:var(--accent-hi)}
.loginbtn:active{transform:scale(.985)}
.loginhint{font-size:11.5px;color:var(--ink-3);margin-top:16px}
#loginFirstHint{margin-top:16px}
:root[data-theme="dark"] .loginbox{background:rgba(28,28,30,.76);
  border-color:rgba(255,255,255,.12);box-shadow:0 40px 100px rgba(0,0,0,.6),
    inset 0 1px 0 rgba(255,255,255,.08)}
:root[data-theme="dark"] .logininput{background:rgba(255,255,255,.06)}
:root[data-theme="dark"] .logininput:focus{background:var(--surface)}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]) .loginbox{background:rgba(28,28,30,.76);
    border-color:rgba(255,255,255,.12);box-shadow:0 40px 100px rgba(0,0,0,.6),
      inset 0 1px 0 rgba(255,255,255,.08)}
  :root:not([data-theme="light"]) .logininput{background:rgba(255,255,255,.06)}
  :root:not([data-theme="light"]) .logininput:focus{background:var(--surface)}
}

/* ---------- 19. Motion ---------- */
@keyframes appleViewIn{
  from{opacity:0;transform:translateY(10px)}
  to{opacity:1;transform:none}
}
@keyframes appleDlgIn{
  from{opacity:0;transform:translateY(10px) scale(.975)}
  to{opacity:1;transform:none}
}
#mailView.on,#orgView.on,#archView.on,#sysView.on,#usersView.on,
#chatView.on,#recruitView.on,#scheduleView.on{
  animation:appleViewIn .38s var(--ease-out) both
}
.detail.swap > *:not(.detail-toolbar){animation:appleViewIn .32s var(--ease-out) both}
.mask.on .dlg{animation:appleDlgIn .3s var(--ease-out) both}
@media (prefers-reduced-motion: reduce){
  #mailView.on,#orgView.on,#archView.on,#sysView.on,#usersView.on,
  #chatView.on,#recruitView.on,#scheduleView.on,
  .detail.swap > *,.mask.on .dlg{animation:none!important}
  .btn,.side button,.row,.tabs button,.seg button,.cw-card,.toast,.sched-ev,
  .chatconv-item,.loginbtn{transition:none!important}
  .btn:active,.side button:active,.loginbtn:active{transform:none!important}
  #chatView .cw-card:hover{transform:none}
}

/* ---------- 20. Responsive (re-declared after the final layer) ---------- */
@media (max-width:1280px){
  body{grid-template-columns:216px 1fr}
  .wrap{grid-template-columns:348px 1fr}
  .search{width:236px}
  .detail{padding:0 24px 44px}
  #chatView.on{grid-template-columns:220px minmax(0,1fr)}
  #recruitView.on,#sysView.on{padding:24px 24px 48px}
  #scheduleView.on{padding:24px 24px 18px}
  .pinned-tabs{margin:0 -24px;padding:8px 24px}
}
@media (max-width:1100px){
  body{grid-template-columns:64px 1fr}
  .side{padding:12px 6px}
  .sbrand{justify-content:center;padding:4px 0 14px}
  .sbrand span,.side button span,.side .sgroup,.side .sfoot .txt,
  .side .sfoot .chev{display:none}
  .side .sfoot .av{display:inline-flex}
  .side .sfoot .acctgear{display:grid}
  .side button{justify-content:center;padding:0}
  .wrap{grid-template-columns:1fr}
  .detail{padding:0 18px 40px}
  .top{height:auto;min-height:56px;flex-wrap:wrap;gap:8px;padding:8px 14px}
  .top .search{flex:1 1 220px;width:auto;min-width:160px}
  .top2{padding:8px 14px}
  .tabbar{padding:8px 14px}
  #chatView.on{grid-template-columns:1fr}
  .chatinput{padding:12px 18px 18px}
  .chatlog{padding:20px 18px}
  #recruitView.on,#sysView.on{padding:20px 16px 40px}
  #scheduleView.on{padding:20px 14px 14px}
  .uwrap{margin:20px 16px 40px}
  .pinned-tabs{margin:0 -18px;padding:8px 18px}
}
@media (max-width:900px){
  #chatView.on{grid-template-columns:1fr}
  .cw-asks{grid-template-columns:1fr}
  .rp-cards,.rp-degrees{grid-template-columns:repeat(2,minmax(0,1fr))}
  .rh-kpis{grid-template-columns:1fr 1fr;gap:16px 0}
}
@media (max-width:720px){
  .dlg{padding:22px;border-radius:20px}
  .dname{font-size:24px}
  .rp-head h2,.sched-title,#chatView .chatwelcome h2{font-size:26px}
  .loginbox{padding:32px 24px 26px}
}
"""
