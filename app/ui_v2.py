"""人才库 v2 —— **独立预览版**（不覆盖原 `ui.py` 的 `_PAGE`）。

设计取向（v2，从逻辑层重做，不沿用旧视觉）：

    这个工具只做三件事：**找人 → 判断合不合适 → 推进他**。
    旧版把三件事平铺在一条很长的卡片流上：每张卡片 250~450px 高、上面压着 6 行筛选控件，
    1080p 一屏看不到一张完整卡片。v2 按"任务的动线"重排：

        ┌──────────────────────────────────────────────────────┐
        │ 人才库  [搜索…]  学历▾ 阶段▾ 档位▾        26 人       │ 52px
        ├───────────────┬──────────────────────────────────────┤
        │ ● 苗雨桐  B   │  苗雨桐  硕士·西安电子科技大学 211   │
        │   硕士·西电   │  档位 [A] [B] [C]    [推进初筛通过]  │
        │ ───────────── │  技能命中 / 缺失（带原文证据）        │
        │ ● 祁亦寒  A   │  系统分析                            │
        │   …           │  操作                                │
        └───────────────┴──────────────────────────────────────┘

    三条设计原则：
    1. **一屏内完成三步**，不在页与页之间跳（左选右看，右直接推进）。
    2. **证据默认可见**，不折叠 —— HR 做取舍看的就是"凭什么是这个人"。
    3. **单一强调色**（墨青）只标"当前选中 / 可点"，其余靠分隔线与字号分层。

⚠️ 这是**预览版**：只实现「人才库」这一屏的主流程（列表 / 筛选 / 详情 / 改档 / 推进阶段 / 头像）。
   邮件、管道、配置等页面仍走旧版。确认方向后再谈替换。
"""
from __future__ import annotations

import json

from . import stages as stages_mod
from . import tiers as tiers_mod

# 占位符只出现一次，用 replace 注入（不用 f-string，避免 JS 里的大括号要转义）。
_PH_STAGES = "__V2_STAGES__"
_PH_TIERS = "__V2_TIERS__"
#: 登录态开关（2026-10-20）：v2 之前不注入，前端因此从不带 X-TP-Token，
#: 设了 TP_AUTH=1 时全页接口 401。现在与原版同一套口径。
_PH_AUTH = "__V2_AUTH__"

_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>人才库 · v2 预览</title>
<style>
:root{
  /* ============ 配色：**完全沿用原版**（`ui.py` 的 :root）============
     业务方 2026-10-20 13:42 明确：「整个系统配色使用原本的系统配色」。
     所以这里不是"新设计"，而是把原版那套令牌**原样搬过来**，保证两个版本观感一致。

     原版配色的三条由来（原注释里写得很清楚，这里照抄结论）：
       ① 页面底是浅灰、卡片是纯白，靠**明度差**把卡片托起来；
       ② 主色是饱和的蓝，只用在「可点 / 当前选中 / 进行中」，面积控制在 5% 以内；
       ③ 中性色偏冷（#F5F6F7 / #DEE0E3 这一族），不用暖灰。
     ⚠️ 色值**不照抄飞书官方**：实测飞书主蓝 #3370FF 对白只有 4.28、弱文字 #8F959E 只有 3.02、
        绿 #34C724 只有 2.25 —— 小字遍地的高密度工具必须守住 4.5。原版已经把每个值算过了。 */
  --ground:#f5f6f7;          /* 原版 paper：页面底 */
  --surface:#ffffff;         /* 卡片/面板 */
  --surface-2:#f2f3f5;       /* 次级面（hover、表头） */
  --nav:#e9ebee;             /* 侧栏：比页面底明显更深，与内容区分开 */
  --ink:#1f2329;             /* 主文字（对白 15.78） */
  --ink-2:#51565d;           /* 次要文字（对白 7.40） */
  --ink-3:#646a73;           /* 弱化文字（对白 5.45） */
  --line:#dee0e3;            /* 分隔线 */
  --line-2:#c9cdd4;          /* 强分隔线 */
  --accent:#3370ff;          /* 主蓝：边框、选中线、图标（只做图形） */
  --accent-ink:#1d5fd6;      /* 深蓝：所有文字形态（对白 5.74） */
  --accent-soft:#f0f5ff;     /* 浅蓝底：按钮底、选中底 */
  --accent-line:#ccdcff;
  --accent-hi:#e1ecff;       /* hover 底 */
  /* 状态色（-soft 底 / -line 描边 / 本身作文字，全部满足 4.5） */
  --ok:#1f7a3d;   --ok-soft:#e8f5eb;   --ok-line:#b8dfc4;
  --warn:#a85b00; --warn-soft:#fff3e0; --warn-line:#f5d5a8;
  --bad:#c93a2e;  --bad-soft:#ffefee;  --bad-line:#f5cfcb;
  /* 档位色：与状态色**分开一套**（B 档不用主蓝，主蓝对白 4.28 不够） */
  --tier-b:#146b9e; --tier-b-soft:#e8f1f8;
  --r:8px; --r-s:6px;
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font:15px/1.58 "PingFang SC","Microsoft YaHei","Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
  display:grid;grid-template-columns:196px 1fr;height:100vh;overflow:hidden;
}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer}
.num{font-variant-numeric:tabular-nums}

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
  cursor:pointer;user-select:none;padding:0 4px}
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
#batchPanel{flex-direction:column;flex-wrap:nowrap;gap:2px;padding:5px;min-width:170px;
  max-width:none;position:fixed;top:0;left:0}
#batchPanel .btn{width:100%;text-align:left;border-color:transparent;background:transparent;
  padding:8px 10px;font-size:13.5px}
#batchPanel .btn:hover{background:var(--accent-soft);border-color:transparent;
  color:var(--accent-ink)}
/* 面板里所有筛选框**同宽同字号**（业务方 2026-10-21：四楞八样的宽度和字号看着乱）。
   `.sel` 原来由文字长度决定宽度，勾选式下拉又自带 min/max-width —— 统一成一把尺子。 */
.morePanel .sel,.morePanel .msel > .msbtn{
  width:172px;min-width:172px;max-width:172px;box-sizing:border-box;flex:none;
  font-size:14px;line-height:1.2}
.morePanel .lbl{font-size:13px;color:var(--ink-3)}
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
/* ---------------- 左侧栏（模块切换） ---------------- */
.side{background:var(--nav);border-right:1px solid var(--line);
  padding:16px 10px;overflow-y:auto;display:flex;flex-direction:column}
/* 品牌区 = 应用图标 + 产品名。图标用内联 data URI：
   这个页面是**单文件内联**的（JS/CSS 全在 HTML 里，见 render_page_v2），
   再挂一个 /logo.png 请求只会多一次往返，还会在服务刚起时闪一下空图。
   base64 是 icon_tray.png 缩到 96px 的版本，四角已透明，浅色侧栏上不露白边。 */
.sbrand{display:flex;align-items:center;gap:9px;font-size:15px;font-weight:600;
  padding:2px 8px 16px;letter-spacing:.01em;line-height:1.25}
.sbrand img{width:26px;height:26px;flex:none;display:block;
  border-radius:7px;box-shadow:0 1px 2px rgba(24,28,32,.14)}
.sbrand span{display:flex;flex-direction:column;min-width:0}
.sbrand b{font-weight:650;color:var(--ink);white-space:nowrap}
.sbrand small{font-size:11.5px;font-weight:400;color:var(--ink-3);
  white-space:nowrap;letter-spacing:.02em;margin-top:1px}
.sgroup{font-size:12px;color:var(--ink-3);padding:10px 12px 5px;letter-spacing:.04em}
.side button{
  display:block;width:100%;text-align:left;border:0;background:transparent;
  color:var(--ink-2);padding:10px 12px;border-radius:var(--r-s);font-size:15px;
  margin-bottom:2px;
}
.sgroup{font-size:13px;}
.side button:hover{background:var(--ground)}
.side button.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
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
  border-radius:var(--r-s);font:inherit;resize:none;outline:none;overflow-y:hidden;
  line-height:1.72;transition:padding-right .15s ease,border-color .15s ease;
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
.asks{display:flex;flex-direction:column;gap:6px;align-items:flex-start}
.askhead{display:flex;align-items:center;gap:8px}
.askhead .btn{margin-left:auto}
.ask{border:1px solid var(--line-2);background:var(--surface);border-radius:14px;
  padding:4px 11px;font-size:13px;color:var(--ink-2);text-align:left}
.ask:hover{border-color:var(--accent);color:var(--accent-ink);background:var(--accent-soft)}
.tracebox{margin-top:8px;font-size:13px}
.tracebox summary{cursor:pointer;color:var(--ink-3)}
.trace{margin-top:4px;padding:6px 9px;background:var(--surface-2);border-radius:5px;
  color:var(--ink-2);line-height:1.7}
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
.proprow-a{margin-top:6px;display:flex;gap:6px}
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
  gap:22px;align-items:start}
.ovcol{min-width:0;background:var(--surface);border:1px solid var(--line);
  border-radius:8px;padding:14px 16px}
.ovcol > h3{margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line);
  font-size:15px;font-weight:650;color:var(--ink)}
.ov-progress .anaring{display:flex;justify-content:center}
.ov-progress .anaringtip{text-align:center;max-width:420px;margin:10px auto 0}
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
/* 页脚只剩构建时间：放大到 14px、主文字色，排障时一眼能念出来 */
.sfoot{margin-top:auto;padding:10px 8px 4px;font-size:14px;color:var(--ink-2);
  font-weight:600;letter-spacing:.01em;line-height:1.6;font-variant-numeric:tabular-nums}

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
body.v-chat .top,body.v-chat .top2,body.v-chat .tabbar,body.v-chat .pipe,body.v-chat .wrap{display:none!important}
#mailView,#orgView,#archView,#sysView,#usersView,#chatView{display:none;flex:1;min-height:0;overflow:hidden}
#usersView.on{display:block;overflow-y:auto}
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
#chatView.on{display:grid;grid-template-columns:1fr 392px}
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
  font-size:13.5px;min-width:0}
.srcbar > .srck{flex:none;color:#4b7a51;font-weight:600}
.srcbar > .srcmail{display:flex;align-items:baseline;gap:8px;min-width:0;flex:1 1 auto;
  overflow:hidden;white-space:nowrap}
/* 邮件主题是这一行里信息量最大的，必须让它先拿到宽度：
   flex:1 1 auto + min-width 保证它不会被发件人/文件名挤成几个字。 */
.srcbar .srcsub{color:#2f5a33;font-weight:600;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:1 1 auto;min-width:180px}
.srcbar .srcmeta{color:#5c7a60;flex:none;font-size:13px;max-width:18ch;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.srcbar > .srcfile{flex:none;display:inline-flex;align-items:center;gap:6px;
  border-left:1px solid #cfe0cc;padding-left:12px;min-width:0}
.srcbar > .srcfile .fn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:#3c6b41;max-width:20ch}
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
.ivrow #ivMode{width:118px}
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
/* 四六级那一行：标签名加粗，值用同一套淡灰可点控件 */
.honosec .cetrow b{color:var(--accent-ink);font-weight:650}
.honosec .cetrow .edv{vertical-align:middle;margin-left:2px}
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
  position:fixed;left:50%;bottom:26px;transform:translateX(-50%);
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
.mask{position:fixed;inset:0;z-index:60;background:rgba(31,35,41,.36);display:none;
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
     失败  "口令不对"    → 横向轻抖，不用读字也知道错在哪
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
   只是允许换行 —— 账号和口令挤在一行会糊成一片。
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
</style>
</head>
<body>
<aside class="side">
  <!-- 品牌区：应用图标 + 产品名。原来的写法是一个 9px 蓝色小方块 + "人才库"，
       那个方块是占位符（`.sbrand i`），跟 exe 图标毫无关系；而"人才库"又是
       下面第一个导航项的名字，顶上一遍、导航里一遍，读起来像重复了。
       现在换成真正的应用图标 + 产品名 + 一行说明（业务方 2026-10-27）。 -->
  <div class="sbrand">
    <img src="__BRAND_ICON__" alt="">
    <!-- 副标题「西北有色院 · 人事」已删（2026-10-28）：产品名本身已经说清是什么，
         这一行在窄侧栏里只会折成两行小字。 -->
    <span><b>HRAgent 工作台</b></span>
  </div>
  <div class="sgroup">招聘</div>
  <button data-view="pool" class="on">人才库</button>
  <button data-view="chat">智能助手</button>
  <button data-view="mail">写邮件</button>
  <div class="sgroup">管理</div>
  <button data-view="org">岗位管理</button>
  <button data-view="archive">归档</button>
  <button data-view="sys">系统配置</button>
  <!-- 账号管理只对 admin 显示（v3.0.3）。权限不足时点进去也是 403，
       但**入口就不该出现** —— 让人点一个"你没权限"的按钮是坏体验。 -->
  <button data-view="users" id="navUsers" hidden>账号管理</button>
  <!-- 构建时间由服务端注入：改了代码后如果这里的时间没变，说明浏览器还在跑旧页面 -->
  <!-- 侧栏页脚只留构建时间（2026-10-28）："v2 预览"是开发期口径，不该出现在给人事用的界面上；
       时间则要能一眼看清（排障第一句就是"你那边版本是什么时候的"），所以放大加粗。 -->
  <div class="sfoot">__V2_BUILD__</div>
</aside>

<div class="main">
<div class="top" id="topBar">
  <div class="brand" style="display:none"><i></i>人才库</div>
  <input id="kw" class="search" placeholder="搜索姓名 / 院校 / 专业 / 技能"
         onkeydown="if(event.key==='Enter'){doSearch()}">
  <!-- 搜索就是**一个输入框 + 一个按钮**，与原版一致（`ui.py:1787-1793`）：
       原来这里有个「关键词 / 按技能 / 语义」下拉，还有一个「具备其一/全部具备」下拉 —
       三个控件在管同一件事，业务方 2026-10-21 明确要求换成一个搜索按钮。
       关键词本身就能命中姓名/院校/专业/技能，够用。 -->
  <button class="btn primary" onclick="doSearch()" style="height:34px;padding:0 20px">搜索</button>
  <!-- 学历/四六级用**勾选式下拉**（业务方 2026-10-21：原生 multiple 太丑，要"都是下拉框"）。
       外观与旁边的单选框一致，点开是带复选框的浮层，仍支持多选。 -->
  <div class="msel" id="msEdu">
    <input type="hidden" id="fEdu">
    <button type="button" class="sel msbtn" onclick="msToggle('msEdu')">学历：不限 ▾</button>
    <div class="msPanel"></div>
  </div>
  <select id="fStage" class="sel"><option value="">阶段：不限</option></select>
  <!-- 档位不在这里再放一个下拉：上面的频道 tab 已经筛同一件事，
       两个入口筛同一个集合只会让人怀疑"为什么筛出来的不一样"。 -->
  <div class="more">
    <button class="btn" id="btnMore" onclick="toggleMore()">更多筛选 ▾</button>
    <div class="morePanel" id="morePanel">
      <select id="fGender" class="sel"><option value="">性别：不限</option></select>
      <select id="fUniv" class="sel"><option value="">院校层次：不限</option></select>
      <select id="fBachelor" class="sel"><option value="">第一学历：不限</option></select>
      <!-- 海外院校是**独立维度**（海外高校既不是 985 也不是 211），
           三态：海外 / 非海外 / 未识别（学校没读到，要人工补录）。原版有，v2 缺。 -->
      <select id="fOverseas" class="sel"><option value="">海外院校：不限</option></select>
      <div class="msel" id="msCet">
        <input type="hidden" id="fCet">
        <button type="button" class="sel msbtn" onclick="msToggle('msCet')">四六级：不限 ▾</button>
        <div class="msPanel"></div>
      </div>
      <!-- 面试部门（原版有、v2 缺）：后端 `interview_unit` 支持顿号/逗号分隔的多值匹配 -->
      <select id="fUnit" class="sel"><option value="">面试部门：不限</option></select>
      <select id="fOutcome" class="sel"><option value="">面试结论：不限</option></select>
      <select id="fDate" class="sel">
        <option value="">投递时间：不限</option>
        <option value="7">近 7 天</option>
        <option value="30">近 30 天</option>
        <option value="year">今年</option>
      </select>
      <div class="clearRow"><button class="btn" onclick="clearFilters()">清空筛选</button></div>
    </div>
  </div>
  <button class="btn" id="btnOverview" onclick="showOverview()">概览 · 待办</button>
  <!-- 当前登录者（只有登录模式才有内容，本机模式保持空） -->
  <div class="whoami" id="whoami"></div>
  <div class="count" id="count"></div>
</div>

<div class="top2">
  <button class="btn btn-primary btn-lg" id="btnIngestMail" onclick="doIngest('mailbox')">收取邮箱简历</button>
  <button class="btn btn-primary btn-lg" id="btnIngestDir" onclick="doIngest('folder')">导入本地文件夹</button>
  <button class="btn btn-primary btn-lg" id="btnUpload" onclick="document.getElementById('upFile').click()"
          title="手机拍的纸质简历照片、扫描件，直接选文件上传入库（不用先改配置目录）">上传简历照片/文件</button>
  <input id="upFile" type="file" multiple hidden
         accept=".jpg,.jpeg,.png,.bmp,.webp,.pdf,.docx,.doc,.txt" onchange="doUpload(this.files)">
  <span class="vdiv"></span>
  <button class="btn btn-green btn-lg" onclick="doExport()">导出名单</button>
  <button class="btn btn-green btn-lg" onclick="doBundle()">下载初筛简历</button>
  <span class="vdiv"></span>
  <label class="allbox"><input type="checkbox" id="chkAll"> 全选</label>
  <button class="btn btn-lg" id="btnArch" onclick="archiveChecked()" disabled>归档勾选（0）</button>
  <button class="btn btn-lg" id="btnCompare" onclick="compareChecked()" disabled>对比档案（0/10）</button>
  <span class="vdiv"></span>
  <button class="btn btn-lg" id="btnPipe" onclick="togglePipe()">投递管道 ▾</button>
  <!-- 「超期设置」已挪进管道面板右上角（业务方 2026-10-21）：
       它只影响管道看板里"停留超 N 天"的标红，是管道自己的参数，
       放在工具栏里既占位置、也让人以为它管的是别的东西。 -->
  <span class="vdiv"></span>
  <button class="btn btn-primary btn-lg" id="btnProp" onclick="toggleProps()">待确认提案</button>
  <!-- 「批量按模型重跑」「分析待分析的人」收进菜单（业务方 2026-10-27 要求
       工具栏必须一行）。这两个是**运维动作**，不是日常动作：一个花钱重跑
       全部模型、一个给历史漏分析的补课，一天也用不上一次，却各占 112px，
       正是它们把工具栏顶到第二行。菜单里保留原文案，行为一字不改。 -->
  <div class="more" id="moreBatch">
    <button class="btn btn-lg" id="btnBatch" onclick="toggleBatchMenu()">批量维护 ▾</button>
    <div class="morePanel" id="batchPanel">
      <button class="btn" onclick="toggleBatchMenu();batchReanalyze()">批量按模型重跑</button>
      <button class="btn" onclick="toggleBatchMenu();analyzePending()">分析待分析的人</button>
    </div>
  </div>
</div>
<div class="tabbar"><div class="tabs" id="tabs"></div></div>

<div id="mailView"></div>
<div id="orgView"></div>
<div id="sysView"></div>
<div id="usersView"></div>
<div id="chatView"></div>
<div id="archView"></div>

<div class="pipe" id="pipe"></div>
<div class="propbox" id="propBox"></div>

<div class="wrap" id="wrapBox">
  <div class="listwrap">
  <div class="list" id="list"></div>
  <div class="pager" id="pager"></div>
</div>
  <div class="detail" id="detail"><div class="hint">读取概览…</div></div>
</div>
</div>
<div class="toast" id="toast"></div>
<div class="mask" id="mask"><div class="dlg" role="dialog" aria-modal="true"><h3 id="dlgTitle"></h3><div class="db" id="dlgBody"></div><div class="db" id="edDlgBody" style="display:none"></div><div class="dbar"><button class="btn" id="dlgNo"></button><button class="btn primary" id="dlgYes"></button></div></div></div>

<!-- 登录页（v3.0.3）。只有 AUTH_ENABLED=true 时才显示；本机模式整块不出现。
     刻意做成**全屏遮罩**而不是独立路由：这个页面本身是单文件内联的，
     多一个 URL 就多一处要跟缓存/刷新较劲的地方。 -->
<div class="loginwrap" id="loginWrap" hidden>
  <!-- 人才星图（v3.2）：canvas 必须在 .loginbox **之前**，这样它自然落在下面；
       CSS 里再给它 z-index:0 / 登录框 z-index:1 兜一层。 -->
  <canvas id="loginNet" aria-hidden="true"></canvas>
  <form class="loginbox" id="loginForm" autocomplete="on">
    <div class="loginbrand"><i class="logindot"></i>HRAgent 工作台</div>
    <!-- 副标题「西北有色院 · 人事」已删（2026-10-28）：同一句话不再出现在界面上 -->
    <label class="loginlabel" for="loginUser">账号</label>
    <input id="loginUser" class="logininput" autocomplete="username" required>
    <label class="loginlabel" for="loginPass">口令</label>
    <input id="loginPass" class="logininput" type="password" autocomplete="current-password" required>
    <div class="loginerr" id="loginErr"></div>
    <!-- 忙碌/成功图标（v3.2）：同一条 SVG，圆环转 → 对勾画出来。
         平时 display:none，只占 14px，不参与布局计算。 -->
    <button class="btn primary loginbtn" type="submit" id="loginBtn">
      <svg class="loginico" viewBox="0 0 16 16" aria-hidden="true">
        <circle cx="8" cy="8" r="6"></circle>
        <path d="M4.6 8.3l2.3 2.3 4.5-4.8"></path>
      </svg>
      <span id="loginBtnText">登录</span>
    </button>
    <!-- 首次使用提示（v3.1.3）：原来这里只有"忘记口令请联系管理员"，
         而**管理员自己也不知道初始口令是什么** —— 双击打开、看到登录页、
         输不进去，第一反应是"程序坏了"。首次部署必须把默认账号写出来。
         口令改过之后这行会自动消失（服务端按 must_change_password 决定是否下发）。 -->
    <div class="loginhint" id="loginFirstHint" hidden></div>
    <div class="loginhint">忘记口令请联系系统管理员重置。</div>
  </form>
</div>

<script>
var STAGE_CFG = __V2_STAGES__;
var TIER_CFG  = __V2_TIERS__;
var ITEMS = [], CUR = null, CUR_DETAIL = null;

/* ---------- 登录态（2026-10-20 补齐） ----------
   v2 之前**只声明了"不支持"**：页面里没有 AUTH_ENABLED、没有 TOKEN，
   `api()` 也就从不注入 X-TP-Token —— 设了 TP_AUTH=1 时全页接口都 401，
   而界面除了顶部一行提示之外没有任何别的线索（`render_page_v2` 里那段）。
   这里照原版（`ui.py:658/700/892/1094`）补齐同一套：
     · AUTH_ENABLED 由服务端按 TP_AUTH 注入（本机模式为 false，请求头照旧不���）
     · TOKEN 取 localStorage.tp_token（登录成功后由谁写入，沿用原版口径）
     · hdr() 给 api() 与裸 fetch 统一加头
     · withToken() 供**浏览器原生导航**用 —— 那条路带不了自定义请求头，
       后端 /api/documents/{id}/file 与 /api/sources/file 都接受 ?token= 兜底。 */
var AUTH_ENABLED = __V2_AUTH__;
var TOKEN = (function(){ try { return localStorage.getItem('tp_token') || ''; }
                        catch(e){ return ''; } })();
/*: 当前登录者（`/api/me` 的结果）。本机模式下保持 null，界面不显示"当前用户"。 */
var ME = null;
function hdr(extra){
  var h = {'Content-Type':'application/json'};
  if(extra) for(var k in extra) if(Object.prototype.hasOwnProperty.call(extra, k)) h[k] = extra[k];
  if(AUTH_ENABLED && TOKEN) h['X-TP-Token'] = TOKEN;
  return h;
}
/* 原生导航（window.open / a[href]）带不了请求头，登录模式只能用 ?token= 兜底 */
function withToken(url){
  if(!(AUTH_ENABLED && TOKEN)) return url;
  return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
}

function esc(s){
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function plainResponse(raw){
  return String(raw || '')
    .replace(/<(script|style)[\s\S]*?<\/\1>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&lt;/gi, '<').replace(/&gt;/gi, '>')
    .replace(/&amp;/gi, '&')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, ' ')
    .replace(/\s+/g, ' ').trim().slice(0, 600);
}
function toast(msg, kind){
  var t = document.getElementById('toast');
  if(!t) return;
  t.textContent = msg;
  t.className = 'toast on ' + (kind || 'info');
  clearTimeout(t._h); t._h = setTimeout(function(){ t.classList.remove('on'); }, 2200);
}
async function api(path, opt){
  opt = opt || {};
  /* 请求头必须在这里统一注入：v2 之前完全没有这层，登录态（TP_AUTH=1）
     下每个接口都会 401，而页面表现为"点了没反应"。 */
  opt.headers = hdr(opt.headers);
  try{
    var r = await fetch(path, opt);
    /* 先取文本再解析：服务端 500 时返回的是 HTML 报错页，直接 r.json() 会把
       真实异常盖成一句"Unexpected token <"，排查时什么都看不见（原版 v1.23.7 踩过）。 */
    var raw = await r.text();
    var j = {};
    try{ j = raw ? JSON.parse(raw) : {}; } catch(e){
      var detailText = plainResponse(raw) || '（空响应）';
      j = {__non_json: true,
           detail: detailText,
           __err: '服务端返回了非 JSON 响应（HTTP ' + r.status + '）'};
    }
    if(!r.ok && !j.__err) j.__err = j.detail || ('HTTP ' + r.status);
    /* 401 = 没登录 / 会话过期 / 被管理员踢下线。统一回到登录页。
       **不能只靠登录时的判断**：会话 12 小时过期，HR 开着页面过夜就会遇到。 */
    if(r.status === 401 && AUTH_ENABLED){
      setToken('');
      ME = null;
      if(!LOGIN_SHOWN) showLogin('登录已失效，请重新登录');
    }
    return j;
  }catch(e){ return {__err: String(e)}; }
}

/* ---------- 顶栏下拉 / 频道 ---------- */
var TAB = 'ALL', SEL = {};
function fillSel(id, label, pairs){
  var el = document.getElementById(id);
  if(!el) return;
  pairs.forEach(function(p){
    var o = document.createElement('option');
    o.value = p[0]; o.textContent = label + '：' + p[1];
    el.appendChild(o);
  });
}
/* ---------- 勾选式多选下拉（业务方 2026-10-21）----------
   为什么不用原生 `<select multiple>`：它渲染成一个灰色列表框 + 滚动条，
   和旁边一排正常下拉完全不搭，用户原话"这样很不美观"。
   这里做成**普通下拉的外观**（一行按钮），点开是带复选框的浮层：
   既保留了多选能力，视觉上又和别的下拉一致。
   选中值用逗号拼进隐藏 input，`query()` 仍按原方式取值，不动查询链路。 */
var MSEL = {};
function msFill(id, label, pairs){
  var wrap = document.getElementById(id);
  if(!wrap) return;
  MSEL[id] = {label: label, pairs: pairs, sel: []};
  var panel = wrap.querySelector('.msPanel');
  if(!panel) return;
  panel.innerHTML = pairs.map(function(p, i){
    return '<label class="msItem"><input type="checkbox" data-i="' + i + '">' +
      '<span>' + esc(p[1]) + '</span></label>';
  }).join('') +
  '<div class="msFoot"><button type="button" class="btn sm" data-msclear="1">清空</button></div>';
  Array.prototype.forEach.call(panel.querySelectorAll('input[type=checkbox]'), function(c){
    c.addEventListener('change', function(){
      msPick(id, Number(c.dataset.i), c.checked);
    });
  });
  var clr = panel.querySelector('[data-msclear]');
  if(clr) clr.addEventListener('click', function(){ msClearOne(id); });
  msSync(id);
}
function msSync(id){
  var m = MSEL[id], wrap = document.getElementById(id);
  if(!m || !wrap) return;
  var btn = wrap.querySelector('.msbtn');
  if(btn){
    btn.textContent = m.sel.length
      ? (m.label + '：' + m.sel.map(function(v){
          var hit = m.pairs.filter(function(p){ return p[0] === v; })[0];
          return hit ? hit[1] : v;
        }).join('、') + ' ▾')
      : (m.label + '：不限 ▾');
    btn.classList.toggle('active', !!m.sel.length);
  }
  var h = wrap.querySelector('input[type=hidden]');
  if(h) h.value = m.sel.join(',');
}
function msPick(id, i, on){
  var m = MSEL[id];
  if(!m) return;
  var v = m.pairs[i] ? m.pairs[i][0] : null;
  if(!v) return;
  var at = m.sel.indexOf(v);
  if(on && at < 0) m.sel.push(v);
  if(!on && at >= 0) m.sel.splice(at, 1);
  msSync(id);
  load();          /* 勾完立刻重查，省得再点一次"搜索" */
}
function msClearOne(id){
  var m = MSEL[id];
  if(!m) return;
  m.sel = [];
  var wrap = document.getElementById(id);
  if(wrap) Array.prototype.forEach.call(wrap.querySelectorAll('input[type=checkbox]'),
    function(c){ c.checked = false; });
  msSync(id);
  load();
}
function msToggle(id){
  var wrap = document.getElementById(id);
  if(!wrap) return;
  var open = wrap.classList.contains('open');
  Array.prototype.forEach.call(document.querySelectorAll('.msel.open'), function(x){
    x.classList.remove('open');
  });
  if(!open) wrap.classList.add('open');
}
/* 点空白关掉所有勾选面板（与"更多筛选"的交互一致） */
document.addEventListener('click', function(ev){
  var t = ev.target;
  if(t && t.closest && t.closest('.msel')) return;
  Array.prototype.forEach.call(document.querySelectorAll('.msel.open'), function(x){
    x.classList.remove('open');
  });
});
(function initBars(){
  msFill('msEdu', '学历', [['大专','大专'],['本科','本科'],['硕士','硕士'],['博士','博士']]);
  fillSel('fStage', '阶段', (STAGE_CFG.order || []).map(function(k){ return [k, k]; }));
  fillSel('fTier', '档位', (TIER_CFG.order || []).map(function(k){
    return [k, k + ' ' + ((TIER_CFG.labels || {})[k] || '')];
  }));
  fillSel('fGender', '性别', [['男','男'],['女','女']]);
  /* ⚠️ 值域必须与后端一致：`univ` 只认 985 / 211 / **特殊** / 未识别
   （`db.py:1562-1573`，其中「特殊」= uni_tier 为"强势"那批强校）。
   这里原来下发的是「强势」—— 不匹配任何分支，后端**直接放行**，
   于是"选了强势"等于没筛，界面却显示已筛选。 */
fillSel('fUniv', '院校层次', [['985','985'],['211','211'],
                  ['特殊','特殊（强校）'],['未识别','未识别']]);
  /* 面试结论：拟录用 / 不录用 / 已签约 / 拒签（值域来自 stages.json 的 outcomes） */
  var _outs = [];
  var _oMap = STAGE_CFG.outcomes || {};
  Object.keys(_oMap).forEach(function(k){ _oMap[k].forEach(function(v){ if(_outs.indexOf(v) < 0) _outs.push(v); }); });
  fillSel('fOutcome', '面试结论', _outs.map(function(v){ return [v, v]; }));
  fillSel('fBachelor', '第一学历', [['985','985'],['211','211'],['特殊','特殊'],['未识别','未识别']]);
fillSel('fOverseas', '海外院校', [['海外','海外'],['非海外','非海外'],['未识别','未识别']]);
  msFill('msCet', '四六级', [['六级','六级'],['四级','四级'],['专业八级','专业八级'],
                             ['专业四级','专业四级'],['考研英语','考研英语'],['未提供','未提供']]);

  /* 频道（原版是顶部 tab）：ALL 走 tier 参数的特殊值，与旧版同一口径 */
  var tiers = (TIER_CFG.order || []).map(function(k){
    return [k, k + ' ' + ((TIER_CFG.labels || {})[k] || '')];
  });
  var tabs = [['ALL','全部']].concat(tiers).concat([
    ['REVIEW','信息缺失'],['UNCONFIRMED','未复核']
  ]);
  var box = document.getElementById('tabs');
  box.innerHTML = tabs.map(function(t){
    return '<button data-tab="' + t[0] + '" class="' + (t[0] === TAB ? 'on' : '') + '">' +
      t[1] + '</button>';
  }).join('');
  Array.prototype.forEach.call(box.querySelectorAll('button'), function(b){
    b.addEventListener('click', function(){
      TAB = b.dataset.tab;
      Array.prototype.forEach.call(box.querySelectorAll('button'), function(x){
        x.classList.remove('on');
      });
      b.classList.add('on');
      load();
    });
  });
})();
/* ---------- 投递管道（第二行折叠；点人名直接打开他的档案） ---------- */
var PIPE_OPEN = false;
function togglePipe(){
  PIPE_OPEN = !PIPE_OPEN;
  document.getElementById('pipe').classList.toggle('on', PIPE_OPEN);
  document.getElementById('wrapBox').classList.toggle('pipe-open', PIPE_OPEN);
  document.getElementById('btnPipe').textContent = PIPE_OPEN ? '收起管道 ▴' : '投递管道 ▾';
  if(PIPE_OPEN) loadPipe();
}
async function loadPipe(){
  var box = document.getElementById('pipe');
  box.innerHTML = '<div class="hint" style="padding:6px">读取管道…</div>';
  var j = await api('/api/pipeline');
  if(j.__err){ box.innerHTML = '<div class="hint" style="padding:6px">' + esc(j.__err) + '</div>'; return; }
  var order = j.stage_order || STAGE_CFG.order || [];
  var stages = j.stages || {};
  /* 管道面板头部：标题在左，「超期设置」在右上角（业务方 2026-10-21）。
     它调的就是这个面板里"停留超 N 天标红"的参数，放在面板里才看得出对应关系；
     原来挂在顶部工具栏上，既占位置也让人以为它管的是别的东西。 */
  box.innerHTML = '<div class="pipehead"><span class="pipeh-t">投递管道</span>' +
      '<button class="btn sm" id="btnOverdue" onclick="setPipeOverdue()" ' +
        'title="设置管道里「停留多少天」算超期（超期的会标红）">超期设置</button>' +
    '</div>' +
    order.map(function(k){
    var col = stages[k] || {count: 0, items: []};
    var all = col.items || [];
    var PER = 5;                     /* 每页 5 人（原版 15，列窄，5 更好读） */
    var pages = Math.max(1, Math.ceil(all.length / PER));
    var cur = Math.min(Math.max(1, PIPE_PAGE[k] || 1), pages);
    PIPE_PAGE[k] = cur;
    var items = all.slice((cur - 1) * PER, cur * PER);
    return '<div class="pcol' + (col.count ? '' : ' zero') + '">' +
      '<h4>' + esc(k) + ' <b class="num">' + (col.count || 0) + '</b></h4>' +
      (items.length
        ? items.map(function(x){
            return '<button class="pchip" onclick="pipeOpen(' + x.candidate_id + ')" ' +
              'title="' + esc((x.job_title || '未归岗') + ' · 停留 ' + (x.days || 0) + ' 天') + '">' +
              esc(x.candidate_name || '') + ' · ' + (x.days || 0) + '天</button>';
          }).join('')
        : '<div class="pmore">—</div>') +
      (pages > 1
        ? '<div class="ppager">' +
            '<button class="pg" onclick="pipePage(\'' + esc(k) + '\',-1)"' +
              (cur <= 1 ? ' disabled' : '') + '>‹</button>' +
            '<span class="num">' + cur + '/' + pages + '</span>' +
            '<button class="pg" onclick="pipePage(\'' + esc(k) + '\',1)"' +
              (cur >= pages ? ' disabled' : '') + '>›</button></div>'
        : '') +
    '</div>';
  }).join('');
}
function pipeOpen(cid){ pick(cid); }
/* 管道每列独立翻页；数据变少时自动夹回有效页 */
function pipePage(stage, delta){
  PIPE_PAGE[stage] = Math.max(1, (PIPE_PAGE[stage] || 1) + delta);
  loadPipe();
}
function toggleMore(){
  document.getElementById('morePanel').classList.toggle('on');
}
/* 「批量维护」动作菜单。与「更多筛选」同一套开合规矩（点外面/按 Esc 收起），
   但两者互斥：两个浮层同时开着会叠在一起，看起来像坏了。
   ⚠️ 面板是 position:fixed：它所在的 `.top2` 有 overflow 裁切（见 CSS 注释），
   所以位置得自己算——贴着按钮左边缘、下方 4px，并夹进视口内。 */
function placeBatchPanel(){
  var p = document.getElementById('batchPanel');
  var b = document.getElementById('btnBatch');
  if(!p || !b || !p.classList.contains('on')) return;
  var r = b.getBoundingClientRect();
  var w = p.offsetWidth || 170;
  /* 按钮靠右时面板会顶出窗口右边：先夹到 [8, innerWidth - w - 8]。
     工具栏可以横向滚动，按钮可能滚到视口外，夹完仍保证看得见。 */
  var left = Math.min(r.left, window.innerWidth - w - 8);
  left = Math.max(8, left);
  p.style.left = left + 'px';
  p.style.top = Math.round(r.bottom + 4) + 'px';
}
function toggleBatchMenu(){
  var p = document.getElementById('batchPanel');
  var m = document.getElementById('morePanel');
  if(m) m.classList.remove('on');
  p.classList.toggle('on');
  if(p.classList.contains('on')) placeBatchPanel();
}
/* 开着的时候窗口缩放 / 任一容器滚动（工具栏自己会横向滚），面板必须跟着走位，
   否则会停在旧坐标上，看起来像"又飘到别处去了"。 */
window.addEventListener('resize', placeBatchPanel);
window.addEventListener('scroll', placeBatchPanel, true);
/* 「更多筛选」是浮层：点**空白处**就该收起（业务方 2026-10-21 反馈）。
   只点按钮才能关的话，用户想关的时候得先瞄准那个按钮——面板还挡住半个页面。
   两条常见做法一起上：① 点浮层外任意处关闭；② Esc 关闭。
   用捕获阶段的 document 监听，并且**跳过面板自身**，否则点下拉会被立刻关掉。 */
document.addEventListener('click', function(ev){
  var panel = document.getElementById('morePanel');
  if(!panel || !panel.classList.contains('on')) return;
  var more = document.getElementById('btnMore');
  if(panel.contains(ev.target) || (more && more.contains(ev.target))) return;
  panel.classList.remove('on');
}, true);
/* 「批量维护」同一套规矩：点面板外或按 Esc 收起，点面板内不关。 */
document.addEventListener('click', function(ev){
  var panel = document.getElementById('batchPanel');
  if(!panel || !panel.classList.contains('on')) return;
  var btn = document.getElementById('btnBatch');
  if(panel.contains(ev.target) || (btn && btn.contains(ev.target))) return;
  panel.classList.remove('on');
}, true);
document.addEventListener('keydown', function(ev){
  if(ev.key !== 'Escape') return;
  var panel = document.getElementById('morePanel');
  if(panel) panel.classList.remove('on');
  var batch = document.getElementById('batchPanel');
  if(batch) batch.classList.remove('on');
});
function clearFilters(){
  ['kw','fEdu','fStage','fTier','fGender','fUniv','fBachelor','fCet','fDate','fOutcome',
   'fOverseas','fUnit'].forEach(function(id){
    var el = document.getElementById(id);
    if(!el) return;
    if(el.multiple){   // 多选下拉必须逐个取消选中，赋空串清不掉
      Array.prototype.forEach.call(el.options, function(o){ o.selected = false; });
    }else{
      el.value = '';
    }
  });
  /* 勾选式下拉的选中状态存在 MSEL 里，hidden input 清空还不够 */
  ['msEdu', 'msCet'].forEach(function(id){
    var m = MSEL[id], wrap = document.getElementById(id);
    if(!m || !wrap) return;
    m.sel = [];
    Array.prototype.forEach.call(wrap.querySelectorAll('input[type=checkbox]'),
      function(c){ c.checked = false; });
    msSync(id);
  });
  TAB = 'ALL';
  Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function(x){
    x.classList.toggle('on', x.dataset.tab === 'ALL');
  });
  load();
}

function query(){
  var p = [];
  function add(k, id){
    var v = document.getElementById(id).value;
    if(v) p.push(k + '=' + encodeURIComponent(v));
  }
  /* 搜索只有一个口径：关键词（后端 `kw` 会同时匹配姓名/院校/专业/技能）。
     原来这里按下拉值分三条路（kw / skills / q=语义），现已统一。 */
  var kw = (document.getElementById('kw').value || '').trim();
  if(kw) p.push('kw=' + encodeURIComponent(kw));
  add('education','fEdu'); add('stage','fStage');
  p.push('page=' + (PAGE.page || 1) + '&page_size=' + (PAGE.page_size || 25));
  add('gender','fGender'); add('univ','fUniv'); add('bachelor','fBachelor'); add('cet','fCet');
  add('outcome','fOutcome');
  /* ⚠️ 这两个参数原来**漏拼了**（业务方 2026-10-21：选「海外院校」点了没反应）。
     界面控件在、change 也绑了 load，但请求里没带参数 —— 结果当然一模一样，
     表现就是"选了等于没选"。控件齐全不代表参数齐全，这条只能靠断点/看请求抓。 */
  add('overseas','fOverseas');
  add('interview_unit','fUnit');
  if(TAB !== 'ALL') p.push('tier=' + encodeURIComponent(TAB));
  var d = document.getElementById('fDate').value;
  if(d){
    var today = new Date();
    var from = new Date(today);
    if(d === 'year'){ from.setMonth(0); from.setDate(1); }
    else from.setDate(today.getDate() - (d === '7' ? 6 : 29));
    var iso = function(x){
      var p2 = function(n){ return (n < 10 ? '0' : '') + n; };
      return x.getFullYear() + '-' + p2(x.getMonth() + 1) + '-' + p2(x.getDate());
    };
    p.push('date_from=' + iso(from));
    p.push('date_to=' + iso(today));
  }
  return p.length ? '?' + p.join('&') : '';
}

/* ---------- 左侧列表 ---------- */
/* 阶段用**各阶段自己的颜色**（config/stages.json 原版就定义好了）：
   全部简历 灰蓝 / 初筛通过 蓝 / 待面试 紫 / 已面试 青绿 / 已发放offer 橙 / 已结束 灰。
   阶段是"他走到哪一步"的唯一视觉线索，全用同一个中性色等于没编码。 */
function stageChip(stage){
  var c = (STAGE_CFG.colors || {})[stage] || '#8a94a6';
  if(!stage) return '';
  return '<span class="stg" style="color:' + c + ';background:' + c + '1f">' + esc(stage) + '</span>';
}
/* 面试结论标签：**紧跟在阶段后面**，一眼分出绿（拟录用/已签约）与红（不录用/拒签）。
   阶段说的是"走到哪一步"，结论说的是"结果如何"—— 两件事，不能混成一个标签。 */
function outcomeChip(o, stage){
  var oc = String(o || '').trim();
  if(!oc) return '';
  /* 原版规则（业务方 2026-10-16 裁定）：结论标签是"这一步的结论"，
     **只在该阶段允许打这个标签时才显示** —— 人一旦走出这个阶段，
     它就不再代表当前状态（显示"拒签"会让人以为他还在 offer 环节）。
     可选标签直接读配置（STAGE_CFG.outcomes），不在前端另写一份映射。 */
  var allowed = (STAGE_CFG.outcomes || {})[String(stage || '').trim()] || [];
  if(allowed.indexOf(oc) < 0) return '';
  var good = (oc === '拟录用' || oc === '已签约');
  return '<span class="ochip ' + (good ? 'g' : 'r') + '">' + esc(oc) + '</span>';
}
/* 投递的岗位 —— HR 筛人时最关心的第二个信息，紧跟学校 */
function jobChip(x){
  if(x.job_title) return '<span class="jchip">' + esc(x.job_title) + '</span>';
  if(x.suggested_job_title) return '<span class="jchip sug">建议 ' + esc(x.suggested_job_title) + '</span>';
  return '<span class="jchip none">未归岗</span>';
}
function rowHtml(x){
  var t = x.tier_effective || '';
  /* 头像**懒加载**（v3.0.1，性能）：一页 25 行，如果每行都在 HTML 里直接写
     `<img src=...>`，浏览器会立刻并发 25 个 `/api/candidates/<id>/avatar`。
     实测 1440px 窗口、25 行里 16 人有照片 → 16 个请求排成一队共 **1471ms**，
     而服务是单进程线程池，这些请求把 `/api/candidates` 和点击详情全堵在后面
     —— 这就是"切换候选人很慢"的直接原因。
     改成 `data-src` + IntersectionObserver：只有**真正滚进视口**的行才发请求，
     而且按行依次发，不抢带宽。`loading="lazy"` 不够：它对 fetch 优先级无效，
     在同一个 viewport 里的 25 张图仍然会一起发。 */
  var av = x.avatar_path === '-' ? ''
    : '<img alt="" data-avatar="' + x.id + '"' +
      ' src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"' +
      ' onerror="this.remove()">';
  var sub = [x.edu_level, x.school].filter(Boolean).join(' · ') || '学历未识别';
  var qual = '';
  if(x.parse_failed){
    qual += '<span class="flagbad" title="简历原文未能解析，需要打开原件人工核对">简历读不出</span>';
  }else if((x.missing_fields || []).length){
    qual += '<span class="flagwarn" title="没抽到：' + esc((x.missing_fields || []).join('、')) +
      '">缺 ' + esc((x.missing_fields || []).join('、')) + '</span>';
  }
  if(x.status_display){
    qual += '<span class="flagneutral" title="' + esc(x.status_hint || '') + '">' +
      esc(x.status_display) + '</span>';
  }
  return '<div class="row' + (CUR === x.id ? ' on' : '') + '" data-id="' + x.id + '">' +
    '<div class="rowbar"></div>' +
    '<div class="chk"><input type="checkbox" data-id="' + x.id + '"' +
      (SEL[x.id] ? ' checked' : '') + '></div>' +
    '<div class="av">' + esc((x.name || '?').slice(0,1)) + av + '</div>' +
    '<div style="min-width:0">' +
      '<div class="nm">' + esc(x.name || '未识别') + stageChip(x.stage) + outcomeChip(x.outcome, x.stage) +
        (x.search_score != null
          ? '<span class="jchip sug">匹配 ' + (Math.round(x.search_score * 100) / 100) + '</span>'
          : '') + qual + '</div>' +
      '<div class="sub">' + esc(sub) + '</div>' +
      '<div class="sub">' + (x.search_hits && x.search_hits.length
        ? x.search_hits.slice(0, 5).map(function(k){ return esc(k); }).join('、')
        : jobChip(x)) + '</div>' +
    '</div>' +
    '<div class="tier ' + esc(t) + '">' + esc(t || '–') + '</div>' +
  '</div>';
}
/* 分页：**默认一页 25 人**（原来不传 page_size → 服务端返回全量，
   页数算出来永远是 1/1，翻页是假的）。 */
var PAGE = {page:1, page_size:25, total:0, total_pages:1};
var FACETS = null;
/* ---------- 分页控件（原来 v2 一屏一屏，没翻页） ---------- */
function renderPager(){
  var box = document.getElementById('pager');
  if(!box) return;
  var total = PAGE.total || 0, pages = PAGE.total_pages || 1, cur = PAGE.page || 1;
  /* 分页条**始终显示**：只有一页时也要能改"每页多少"（否则 25 人正好一页、
     下面只有一行分页文字，连每页选择器都没有，想翻页的人没处下手）。
     翻页按钮在边界时置灰。 */
  box.innerHTML =
    '<button class="btn sm" onclick="gotoPage(' + (cur - 1) + ')"' + (cur <= 1 ? ' disabled' : '') + '>上一页</button>' +
    '<span class="num">第 ' + cur + ' / ' + pages + ' 页　共 ' + total + ' 人</span>' +
    '<button class="btn sm" onclick="gotoPage(' + (cur + 1) + ')"' + (cur >= pages ? ' disabled' : '') + '>下一页</button>' +
    '<span class="hint">跳至</span>' +
    '<input class="pgjump" id="pgJump" value="' + cur + '" ' +
      'onkeydown="if(event.key===\'Enter\'){gotoPage(parseInt(document.getElementById(\'pgJump\').value)||1);}" ' +
      'style="width:52px;height:26px;text-align:center">' +
    '<span class="hint">页</span>' +
    '<span class="pgwrap">每页' +
      '<select class="sel" style="height:26px" onchange="setPageSize(this.value)">' +
        [25, 50, 100, 200].map(function(n){
          return '<option value="' + n + '"' + (String(PAGE.page_size) === String(n) ? ' selected' : '') + '>' + n + '</option>';
        }).join('') + '</select>人</span>';
}
function gotoPage(p){
  if(p < 1 || (PAGE.total_pages && p > PAGE.total_pages)) return;
  PAGE.page = p;
  load();
  document.getElementById('list').scrollTop = 0;
}
function setPageSize(n){
  PAGE.page_size = Number(n) || 20;
  PAGE.page = 1;
  load();
}
/* ---------- 筛选器上的人数（facets）---------- */
function renderFacetCounts(){
  if(!FACETS) return;
  var map = {
    fGender: FACETS.gender_facets, fUniv: FACETS.uni_facets,
    fBachelor: FACETS.bachelor_facets, fCet: FACETS.cet_facets,
    fStage: FACETS.stage_facets, fOverseas: FACETS.overseas_facets
  };
  Object.keys(map).forEach(function(id){
    var fac = map[id];
    if(!fac) return;
    var sel = document.getElementById(id);
    /* ⚠️ 学历/四六级现在是**勾选式下拉**，对应元素是 hidden input，
       没有 `.options` —— 直接 forEach 会抛 TypeError。人数改由 msFacet 写进浮层标签。 */
    if(!sel || !sel.options) return;
    Array.prototype.forEach.call(sel.options, function(op){
      var c = fac[op.value];
      if(c === undefined) return;
      op.textContent = op.textContent.replace(/（\d+）$/, '') + '（' + c + '）';
    });
  });
  msFacet('msEdu', FACETS.edu_facets);
  msFacet('msCet', FACETS.cet_facets);
}
/* 把 facet 人数写到勾选式下拉的每一项后面 */
function msFacet(wrapId, fac){
  var m = MSEL[wrapId], wrap = document.getElementById(wrapId);
  if(!m || !wrap || !fac) return;
  var spans = wrap.querySelectorAll('.msItem span');
  Array.prototype.forEach.call(spans, function(sp, i){
    var p = m.pairs[i];
    if(!p) return;
    var c = fac[p[0]];
    sp.textContent = p[1] + (c === undefined ? '' : '（' + c + '）');
  });
}
/* 列表事件**委托在 #list 上，只绑一次**（v3.0.3 性能）。
   原来 renderList 每次都给 25 行各绑 2 个监听器（行点击 + checkbox），
   等于每次渲染创建 50 个闭包。而 renderList 会在 load / pick / showOverview
   三处被调用 —— 只是"点一个人换个高亮"也要重建 50 个监听器，纯属浪费。
   委托的另一个好处：行是 innerHTML 重建的，委托不受重建影响。 */
function bindListOnce(){
  var el = document.getElementById('list');
  if(!el || el.__bound){ return; }
  el.__bound = true;
  el.addEventListener('click', function(ev){
    var chk = ev.target.closest ? ev.target.closest('.chk') : null;
    if(chk){ ev.stopPropagation(); return; }        /* 勾选框自己处理，不算"选人" */
    var row = ev.target.closest ? ev.target.closest('.row') : null;
    if(row && row.dataset.id){ pick(Number(row.dataset.id)); }
  });
  el.addEventListener('change', function(ev){
    var c = ev.target;
    if(!c || c.type !== 'checkbox' || !c.dataset.id){ return; }
    SEL[c.dataset.id] = c.checked;
    syncSel();
  });
}

function renderList(){
  var el = document.getElementById('list');
  var total = (PAGE && PAGE.total != null) ? PAGE.total : ITEMS.length;
  document.getElementById('count').innerHTML =
    '共 <b class="num">' + total + '</b> 人';
  if(!ITEMS.length){ el.innerHTML = '<div class="empty">没有符合条件的候选人</div>'; return; }
  el.innerHTML = ITEMS.map(rowHtml).join('');
  bindListOnce();
  hydrateAvatars(el);
  var all = document.getElementById('chkAll');
  if(all){ all.checked = ITEMS.length > 0 && ITEMS.every(function(x){ return SEL[x.id]; }); }
}
/* 只切换列表里的高亮行，不重建列表（v3.0.3）。
   pick() 原来第一件事就是 renderList()，而那会重写 25 行 HTML、
   重置头像队列（已经加载好的头像全丢）、并且发生在一个网络请求之前 ——
   用户在"点击 → 详情出现"这段时间里看到的就是列表整块闪一下。 */
function highlightRow(id){
  var el = document.getElementById('list');
  if(!el) return;
  var old = el.querySelector('.row.on');
  if(old) old.classList.remove('on');
  if(id == null) return;
  var now = el.querySelector('.row[data-id="' + id + '"]');
  if(now){ now.classList.add('on'); }
  else { renderList(); }      /* 不在当前页（比如从搜索/对比跳进来）才退回整块重渲染 */
}

/* 头像懒加载：行滚进视口才去取图（见 rowHtml 里的说明）。
   同一批里**一次只发一个**请求，前一个结束再发下一个——单进程服务扛不住
   25 个并发连接同时排队。滚出去的会取消，不浪费请求。 */
var AV_QUEUE = [], AV_BUSY = false, AV_PENDING = [];
/* 为什么不用 IntersectionObserver：它在**不活跃的标签页 / 无头渲染**里
   根本不回调（实测本机 Chromium 里 observe 之后一次都不触发），
   表现就是"头像永远不出现"。用 getBoundingClientRect 主动判定：
   视口判定是同步、可靠的，滚动时再扫一遍即可。 */
/* 判定"这一行现在看得见吗"必须用**列表容器自己的可视区**，不能用窗口。
   列表是 `overflow-y:auto` 的独立滚动容器：25 行加起来 1700 多像素，
   而窗口只有 900。用 window.innerHeight 判会把第 8 行以后全判成"在视口外"
   （实测 top=1104 > 900），于是滚到哪儿都不加载——头像永远不出现。
   容器 rect 与元素 rect 同一坐标系，直接比就是对的。 */
function avInView(im){
  var b = im.getBoundingClientRect();
  if(b.width === 0 && b.height === 0) return false;
  /* 判定容器要和这张图**属于同一个滚动体系**：列表里的图用 #list，
     详情里的图用 #detail（它自己也滚动）。用错容器会出现两种情况之一：
     列表后面的行永远不加载，或者详情页的头像永远不出现。 */
  var box = im.closest ? (im.closest('#list') || im.closest('#detail')) : null;
  var vb = box ? box.getBoundingClientRect() : null;
  if(!vb || vb.height === 0){
    vb = {top: 0, bottom: window.innerHeight || document.documentElement.clientHeight};
  }
  var pad = 200;                    /* 提前两行加载，滚动时不会看到"空白再补上" */
  return b.bottom > vb.top - pad && b.top < vb.bottom + pad;
}
function hydrateAvatars(root){
  var imgs = (root || document).querySelectorAll('img[data-avatar]');
  if(!imgs.length) return;
  /* **合并**而不是覆盖（v3.0.3）：列表和详情各自调一次，覆盖会让先注册的
     那批（比如列表里还没滚到的行）永远丢失，表现为"列表滚下去头像不出现"。 */
  var keep = AV_PENDING.filter(function(im){ return im.isConnected && !im.__avDone; });
  AV_PENDING = keep.concat(Array.prototype.slice.call(imgs));
  /* 必须**补两次延迟扫描**：renderList 是紧接着 innerHTML 赋值调进来的，
     那一刻布局还没落定，getBoundingClientRect 全是 0 → 全部判成"不在视口"
     → 一张头像都不加载（实测就是这个症状：pending 有 16 个、queue 永远是 0）。
     同步扫一次兜住"布局已完成"的情况，再各补一次等布局/字体/滚动位置稳定。 */
  [0, 60, 320].forEach(function(d){
    if(d) setTimeout(avScan, d); else avScan();
  });
}
function avScan(){
  if(!AV_PENDING.length) return;
  var rest = [];
  AV_PENDING.forEach(function(im){
    if(!im.isConnected || im.__avDone){ return; }   /* 行被重渲染掉了 / 已处理过 */
    if(avInView(im)){ AV_QUEUE.push(im); } else { rest.push(im); }
  });
  AV_PENDING = rest;
  avPump();
}
/* 滚动时再扫（节流 120ms）。
   ⚠️ 滚动事件**不冒泡**，`document` 上的 capture 监听在部分浏览器里
   也收不到滚动容器的滚动（实测本机 Chromium 滚到底部一次都不触发）。
   所以必须**直接绑到滚动容器 #list 上**，window 那份只是兜住整页滚动。 */
(function(){
  var t = null;
  function onScroll(){
    if(t) return;
    t = setTimeout(function(){ t = null; avScan(); }, 120);
  }
  window.addEventListener('scroll', onScroll, {passive: true});
  function bindList(){
    /* 列表和详情**各自**是滚动容器，两个都要绑（详情里的头像也要按滚动加载） */
    ['list', 'detail'].forEach(function(id){
      var el = document.getElementById(id);
      if(el && !el.__avBound){ el.__avBound = true; el.addEventListener('scroll', onScroll, {passive: true}); }
    });
  }
  bindList();
  document.addEventListener('DOMContentLoaded', bindList);
  window.__avBindList = bindList;      /* renderList 重建列表后调用一次 */
})();
function avPump(){
  if(AV_BUSY) return;
  var im = AV_QUEUE.shift();
  if(!im) return;
  AV_BUSY = true;
  var done = function(){
    AV_BUSY = false;
    AV_QUEUE.length ? setTimeout(avPump, 0) : null;
  };
  avLoad(im, done);
}
function avLoad(im, done){
  var cid = im.dataset.avatar;
  if(!cid){ if(done) done(); return; }
  var url = withToken('/api/candidates/' + cid + '/avatar');
  /* ⚠️ 不能用 HEAD 预检：FastAPI 的 `@app.get` 路由**不响应 HEAD**
     （返回 405 Method Not Allowed，实测 25 张图全被当成失败移除）。
     直接用 Image 加载：浏览器自己会走缓存，onload/onerror 就是结果。
     抽图还没好的返回 404（后台在抽），此时不重试 —— 等下次列表刷新自然出现；
     重试会把队列堵住，正是我们要消除的东西。 */
  im.__avDone = true;          /* 标记已处理：列表和详情都调 avScan，不能重复请求 */
  var probe = new Image();
  probe.onload = function(){
    im.src = url;
    im.classList.add('in');
    if(done) done();
  };
  probe.onerror = function(){
    im.remove();
    if(done) done();
  };
  probe.src = url;
}

/* ---------- 右侧详情 ---------- */
function tierSeg(d){
  var t = d.tier_final || d.tier_suggested || '';
  return '<div class="seg">' + (TIER_CFG.order || []).map(function(k){
    return '<button data-tier="' + esc(k) + '" class="' + (k === t ? 'on' : '') + '">' +
      esc(k) + ' ' + esc((TIER_CFG.labels || {})[k] || '') + '</button>';
  }).join('') + '</div>';
}
/* ============================================================
   右栏两种视图：概览（统计 + 今日待办）／完整档案（可编辑）
   ============================================================ */
var MODE = 'detail';
var COMPARE_MAX = 10;
var COMPARE_IDS = [];
var COMPARE_DATA = {};
var COMPARE_ACTIVE = null;

/* ---------- 概览 ---------- */
async function showOverview(){
  MODE = 'overview'; CUR = null;
  COMPARE_IDS = []; COMPARE_DATA = {}; COMPARE_ACTIVE = null;
  renderList();
  var box = document.getElementById('detail');
  box.classList.remove('comparing');
  box.innerHTML = '<div class="hint">读取概览…</div>';
  /* 两个请求互不依赖，必须并行（v3.0.3）。原来是串行 await，
     等于白白多等一个完整往返 —— 而这是**打开页面第一眼**看到的东西。 */
  var _ov = await Promise.all([api('/api/stats'), api('/api/brief/today')]);
  var st = _ov[0], br = _ov[1];
  if(st.__err){ box.innerHTML = '<div class="hint">' + esc(st.__err) + '</div>'; return; }
  var tiers = st.tiers || {}, stages = st.stages || {};
  function num(label, v, hint){
    return '<div class="stat"><div class="stat-n num">' + v + '</div>' +
      '<div class="stat-l">' + label + '</div>' +
      (hint ? '<div class="stat-h">' + hint + '</div>' : '') + '</div>';
  }
  anaLoad(st);               /* 复用上面刚拿到的 stats，不再重复请求 */
  var todo = (br.priorities || []).map(function(x, i){
    return '<div class="todo"><div class="todo-i num">' + (i + 1) + '</div>' +
      '<div style="min-width:0"><div class="todo-t">' + esc(x.title || '') + '</div>' +
      (x.why ? '<div class="todo-w">' + esc(x.why) + '</div>' : '') +
      (x.action ? '<div class="todo-a">' + esc(x.action) + '</div>' : '') + '</div></div>';
  }).join('');
  box.innerHTML =
    '<div class="dhead"><div style="min-width:0">' +
      '<div class="dname">今日概览</div>' +
      '<div class="dmeta"><span>' + esc(br.date || '') + '　' +
        (br.source === 'llm' ? '模型判断版' : '规则版') +
        (br.generated_at ? '　生成于 ' + esc(String(br.generated_at).slice(11, 16)) : '') +
      '</span></div></div>' +
      '<div class="dact">' +
        '<button class="btn" onclick="rebuildBrief()">重算待办</button>' +
        '<button class="btn" onclick="runScan()">主动扫描</button>' +
      '</div>' +
    '</div>' +

    /* 第一行：投递进度（环）+ 库存 —— **并排**，一屏多看一半信息 */
    '<div class="sec ovrow">' +
      '<div class="ovcol ov-progress"><h3>投递进度</h3>' +
        '<div class="anaring">' + stageRing(stages, {size: 190}) +
        '</div></div>' +
      '<div class="ovcol"><h3>今日待办' +
        (br.total ? '（' + br.total + '）' : '') + '</h3>' +
        (br.headline ? '<div class="sum">' + esc(br.headline) + '</div>' : '') +
        (todo ? '<div style="margin-top:10px">' + todo + '</div>'
              : '<div class="hint">今天没有需要优先处理的事</div>') +
        (br.disclaimer ? '<div class="ev">' + esc(br.disclaimer) + '</div>' : '') +
      '</div>' +
    '</div>' +

    /* 下方只保留一整个自助分析区，图表和条件使用整栏宽度。 */
    '<div class="sec ov-analysis"><h3>自助分析（勾条件 → 自动出图）' +
      '<span class="hint" id="anaScope" style="margin-left:8px">统计范围：全部候选人</span>' +
      '</h3><div id="anaCustom"></div></div>' +
    '';
  document.getElementById('count').innerHTML =
    '共 <b class="num">' + st.people + '</b> 人';
}
async function rebuildBrief(){
  toast('重算今日待办…');
  await api('/api/brief/rebuild?use_llm=1', {method:'POST'});
  showOverview();
}

/* ---------- 多档案对比 ----------
   勾选 2-5 人后，每个人渲染成独立只读窗口：横向并排、窗口内各自滚动。
   编辑仍回到单人完整档案，避免多个表单共用 id 互相覆盖。 */
function compareHonorText(h){
  if(h == null) return '';
  if(typeof h === 'string') return h;
  if(typeof h !== 'object') return String(h);
  return String(h.evidence || h.name || h.title || h.text || '').trim();
}

function compareCard(d){
  var apps = d.applications || [];
  var app0 = apps[0] || {};
  var row0 = ITEMS.filter(function(x){ return x.id === d.id; })[0] || {};
  var stage = d.stage || app0.stage || '';
  var tier = d.tier_effective || app0.tier_final || app0.tier_suggested || '';
  /* 详情页头像同样走懒加载（v3.0.3）：与列表用同一套 data-avatar 机制，
     避免"列表已改成懒加载、详情还是一次性请求"两套逻辑漂移。 */
  var av = d.avatar_path === '-' ? ''
    : '<img alt="" data-avatar="' + d.id + '"' +
      ' src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"' +
      ' onerror="this.remove()">';
  var edu = (d.edu_history || []).filter(function(h){ return h && h.school; })
    .map(function(h){
      return '<div class="edu-line"><span class="edu-degree">' + esc(h.degree || '学历') +
        '</span><span class="edu-school">' + esc(h.school) + '</span>' +
        (h.major ? '<span class="edu-major">' + esc(h.major) + '</span>' : '') +
        (h.rank_no ? '<span class="rk">软科第 ' + h.rank_no + '</span>' : '') + '</div>';
    }).join('') || '<div class="hint">未识别到教育经历</div>';
  var skills = (d.skills || []).slice(0, 30).map(function(s){
    return '<span class="compare-skill' + (s.verified ? '' : ' unverified') + '">' +
      esc(s.name) + (s.verified ? '' : '（未核验）') + '</span>';
  }).join('') || '<span class="hint">无技能记录</span>';
  var honors = d.honors || {};
  var honorText = ['论文', '专利', '荣誉', '奖学金'].map(function(k){
    var arr = (honors[k] || []).map(compareHonorText).filter(Boolean);
    return arr.length ? '<div class="ev"><b>' + esc(k) + '</b>：' +
      esc(arr.slice(0, 6).join('；')) + '</div>' : '';
  }).join('') || '<div class="hint">无记录</div>';
  var insight = d.insight || {};
  var emailState = d.email_state || {};
  var interview = [
    d.interview_unit ? '单位：' + d.interview_unit : '',
    d.interview_date ? '日期：' + String(d.interview_date).slice(0, 16) : '',
    d.interview_slot ? '时段：' + d.interview_slot : '',
    d.interview_mode ? '方式：' + d.interview_mode : '',
    d.outcome ? '结论：' + d.outcome : ''
  ].filter(Boolean).map(esc).join('<br>') || '<span class="hint">未登记面试安排</span>';
  var docs = (d.resume_documents || []).map(function(x){
    return '<div class="doc"><span class="doc-n">' + esc(x.file_name || '') + '</span>' +
      '<span class="doc-a"><button class="btn sm" onclick="openDoc(' + x.id + ',1)">打开</button>' +
      '<button class="btn sm" onclick="openDoc(' + x.id + ',0)">下载</button></span></div>';
  }).join('') || '<div class="hint">无简历原件</div>';

  return '<article class="compare-card" id="compareCard_' + d.id + '">' +
    '<div class="compare-card-head">' +
      '<div class="av-lg" style="width:40px;height:40px;font-size:15px">' +
        esc((d.name || '?').slice(0, 1)) + av + '</div>' +
      '<div class="compare-card-title"><div class="nm">' + esc(d.name || '未识别') + '</div>' +
        '<div class="dmeta">' + stageChip(stage) + outcomeChip(d.outcome, stage) +
          (tier ? '<span class="tier ' + esc(tier) + '">' + esc(tier) + '</span>' : '') +
        '</div></div>' +
      '<button class="compare-close" title="关闭这个档案窗口" ' +
        'onclick="closeCompareCard(' + d.id + ')">×</button>' +
    '</div>' +
    '<div class="compare-card-body">' +
      '<div class="sec"><h3>基本信息</h3><dl class="compare-metric">' +
        '<dt>学历</dt><dd>' + esc(d.edu_level || '—') + '</dd>' +
        '<dt>学校 / 专业</dt><dd>' + esc([d.school, d.major].filter(Boolean).join(' / ') || '—') + '</dd>' +
        '<dt>性别</dt><dd>' + esc(d.gender || '—') + '</dd>' +
        '<dt>电话</dt><dd>' + esc(d.phone && d.phone !== '—' ? d.phone : '—') + '</dd>' +
        '<dt>邮箱</dt><dd>' + esc(d.email && d.email !== '—' ? d.email : '—') + '</dd>' +
        '<dt>岗位</dt><dd>' + esc(app0.job_title || '未归岗') + '</dd>' +
        '<dt>状态</dt><dd>' + esc(d.status_display || app0.status_display || '—') + '</dd>' +
      '</dl></div>' +
      '<div class="sec"><h3>教育经历</h3>' + edu + '</div>' +
      '<div class="sec"><h3>技能与证据</h3><div class="skill">' + skills + '</div>' +
        ((d.skills || []).filter(function(s){ return s.evidence; }).slice(0, 3)
          .map(function(s){ return '<div class="ev"><b>' + esc(s.name) + '</b>：' +
            esc(s.evidence) + '</div>'; }).join('')) + '</div>' +
      '<div class="sec"><h3>荣誉 / 论文 / 专利</h3>' + honorText + '</div>' +
      '<div class="sec"><h3>系统分析</h3>' +
        (insight.summary ? '<div class="sum">' + esc(insight.summary) + '</div>' : '<div class="hint">暂无分析</div>') +
        ((insight.risks || []).length ? '<div class="ev">风险：' + esc(insight.risks.join('；')) + '</div>' : '') +
      '</div>' +
      '<div class="sec"><h3>面试与录用</h3><div class="ev">' + interview + '</div>' +
        '<div class="hint" style="margin-top:6px">字段抽取：' +
          esc(app0.extract_mode || '未记录') +
          (emailState.sent ? '　已发邮件 ' + esc(String(emailState.count || 0)) + ' 次' : '') +
        '</div></div>' +
      '<div class="sec"><h3>简历原件</h3>' + docs + '</div>' +
      '<div class="sec"><h3>备注</h3><div class="ev">' +
        esc(d.note || '无备注') + '</div></div>' +
      '<div class="bar2" style="margin-top:14px">' +
        '<button class="btn primary" onclick="openCompareFull(' + d.id + ')">打开完整档案</button>' +
      '</div>' +
    '</div></article>';
}

function renderCompare(){
  if(MODE !== 'compare') return;
  if(!COMPARE_ACTIVE || !COMPARE_DATA[COMPARE_ACTIVE]){
    COMPARE_ACTIVE = Number(COMPARE_IDS[0] || 0) || null;
  }
  if(COMPARE_ACTIVE) openPinnedProfile(COMPARE_ACTIVE);
}

function pinnedTabsHtml(){
  return '<div class="pinned-tabs"><div class="pinned-tab-list">' +
    COMPARE_IDS.map(function(id){
      var d = COMPARE_DATA[id] || {};
      return '<div class="pinned-tab' + (Number(id) === Number(COMPARE_ACTIVE) ? ' on' : '') +
        '" title="' + esc(d.name || '未识别') + '" onclick="activatePinnedTab(' + id + ')">' +
        '<span class="pt-name">' + esc(d.name || '未识别') + '</span>' +
        '<button class="pt-x" title="关闭这个档案" onclick="event.stopPropagation();closeCompareCard(' +
          id + ')">×</button></div>';
    }).join('') +
    '</div><div class="pinned-tab-actions"><span class="hint">' +
      COMPARE_IDS.length + ' / ' + COMPARE_MAX +
    '</span><button class="btn danger" onclick="closeCompareAll()">全部关闭</button></div></div>';
}

function openPinnedProfile(cid){
  var d = COMPARE_DATA[cid];
  if(!d) return;
  COMPARE_ACTIVE = Number(cid);
  CUR = Number(cid);
  CUR_DETAIL = d;
  MODE = 'compare';
  highlightRow(CUR);         /* 同 pick：只换高亮，不重建列表 */
  MULTI_IVUNIT = String(d.interview_unit || '').split(/[、，,;；]+/)
    .map(function(x){ return x.trim(); }).filter(Boolean);
  var box = document.getElementById('detail');
  box.classList.add('comparing');
  box.innerHTML = pinnedTabsHtml() +
    '<div class="pinned-profile" id="pinnedProfile">' + fullDetail(d) + '</div>';
  multiRenderIvUnit();
  bindDetail(d);
  edInit(document.getElementById('pinnedProfile'));
  loadAudit(d.id);
  CUR_SNAP = formSnapshot(d);
  box.scrollTop = 0;
  box.classList.remove('swap'); void box.offsetWidth; box.classList.add('swap');
  killAutofill();
}

function activatePinnedTab(cid){
  if(!COMPARE_DATA[cid]) return;
  openPinnedProfile(cid);
}

async function compareChecked(){
  var ids = Object.keys(SEL).filter(function(k){ return SEL[k]; }).map(Number);
  if(ids.length < 2){ toast('请至少勾选 2 位候选人', 'warn'); return; }
  if(ids.length > COMPARE_MAX){
    toast('最多固定 ' + COMPARE_MAX + ' 个完整档案，请减少勾选', 'warn');
    return;
  }
  COMPARE_IDS = ids;
  COMPARE_DATA = {};
  MODE = 'compare'; CUR = null;
  renderList();
  var box = document.getElementById('detail');
  box.innerHTML = '<div class="hint">正在读取 ' + ids.length + ' 份完整档案…</div>';
  var rows = await Promise.all(ids.map(function(id){
    return api('/api/candidates/' + id);
  }));
  rows.forEach(function(d, i){
    if(d && !d.__err) COMPARE_DATA[ids[i]] = d;
  });
  COMPARE_IDS = ids.filter(function(id){ return !!COMPARE_DATA[id]; });
  if(!COMPARE_IDS.length){
    box.innerHTML = '<div class="miss">档案读取失败，请稍后重试</div>';
    return;
  }
  COMPARE_ACTIVE = Number(COMPARE_IDS[0]);
  openPinnedProfile(COMPARE_ACTIVE);
}

function closeCompareCard(cid){
  var oldActive = Number(COMPARE_ACTIVE);
  var oldIndex = COMPARE_IDS.findIndex(function(x){ return Number(x) === Number(cid); });
  COMPARE_IDS = COMPARE_IDS.filter(function(x){ return Number(x) !== Number(cid); });
  delete COMPARE_DATA[cid];
  if(!COMPARE_IDS.length){ closeCompareAll(); return; }
  if(oldActive === Number(cid)){
    COMPARE_ACTIVE = Number(COMPARE_IDS[
      Math.max(0, Math.min(oldIndex, COMPARE_IDS.length - 1))
    ]);
    openPinnedProfile(COMPARE_ACTIVE);
  }else{
    openPinnedProfile(COMPARE_ACTIVE);
  }
}

function closeCompareAll(){
  COMPARE_IDS = [];
  COMPARE_DATA = {};
  COMPARE_ACTIVE = null;
  MODE = 'overview';
  showOverview();
}

function openCompareFull(cid){
  COMPARE_IDS = [];
  COMPARE_DATA = {};
  pick(cid);
}

/* ---------- 完整档案 ---------- */
function opt(v, cur, lab){
  return '<option value="' + esc(v) + '"' + (String(cur || '') === String(v) ? ' selected' : '') +
    '>' + esc(lab === undefined ? (v || '—') : lab) + '</option>';
}
function input(id, val, ph, w){
  return '<input id="' + id + '" value="' + esc(val || '') + '" placeholder="' + esc(ph || '') +
    '" style="width:' + (w || 150) + 'px">';
}
/* ---------- 淡灰可点击字段（点一下才变输入框）----------
   为什么：档案里常驻一排编辑框，看着像"待填的表单"，其实多数时候只是"看一眼"（业务方 2026-10-21）。
   做法：默认显示淡灰文本，点击才把输入框显示出来；失焦/回车写回文本，Esc 还原。
   ⚠️ 真实的 input/select **一直存在**（只是 CSS 隐藏），**id 也完全不变** ——
   `formSnapshot`/`formDiff`/`saveAll` 全都按 id 取值，这条链路一行都不用改。 */
function edText(id, val, ph, w, type){
  var v = val == null ? '' : String(val);
  return '<span class="edv' + (v ? '' : ' empty') + '" data-for="' + id + '"' +
    (w ? ' style="min-width:' + w + 'px"' : '') + '>' +
    '<span class="edv-txt">' + esc(v) + '</span></span>' +
    '<input id="' + id + '" data-ed="1" type="' + (type || 'text') + '" value="' + esc(v) +
    '" placeholder="' + esc(ph || '') + '"' +
    (w ? ' style="width:' + w + 'px;display:none"' : ' style="display:none"') + '>';
}
/* opts: [[value, label], ...]；cur 为当前值 */
function edSel(id, opts, cur, w){
  var lab = '';
  opts.forEach(function(o){ if(String(o[0]) === String(cur)) lab = o[1]; });
  return '<span class="edv' + (cur ? '' : ' empty') + '" data-for="' + id + '"' +
    (w ? ' style="min-width:' + w + 'px"' : '') + '>' +
    '<span class="edv-txt">' + esc(cur ? lab : '') + '</span></span>' +
    '<select id="' + id + '" data-ed="1"' +
    (w ? ' style="width:' + w + 'px;display:none"' : ' style="display:none"') + '>' +
    opts.map(function(o){
      return '<option value="' + esc(o[0]) + '"' +
        (String(o[0]) === String(cur) ? ' selected' : '') + '>' + esc(o[1]) + '</option>';
    }).join('') + '</select>';
}
function edCommit(el){
  var sp = document.querySelector('.edv[data-for="' + el.id + '"]');
  if(!sp || sp.dataset.open !== '1') return;
  sp.dataset.open = '';
  var v = el.value;
  var tx = sp.querySelector('.edv-txt');
  if(tx) tx.textContent = v;
  sp.classList.toggle('empty', !v);
  sp.style.display = '';
  el.style.display = 'none';
}
function edInit(root){
  root = root || document.getElementById('detail');
  if(!root) return;
  Array.prototype.forEach.call(root.querySelectorAll('.edv[data-for]'), function(sp){
    sp.addEventListener('click', function(){
      var el = document.getElementById(sp.dataset.for);
      if(!el || sp.dataset.open === '1') return;
      sp.dataset.open = '1';
      sp.style.display = 'none';
      el.style.display = '';
      /* 记下**进入编辑时**的值，供 Esc 还原。
         ⚠️ 原来只有 Esc 分支读 `el.dataset.orig`、却从没人写过它 ——
         按 Esc 会取到 undefined → 清成空串 → blur 写回 span → 「保存更正」把字段清空。
         每次点开都重记一次，才不会还原成上一次的旧值。 */
      el.dataset.orig = el.value;
      el.focus();
      if(el.select) el.select();
    });
  });
  Array.prototype.forEach.call(root.querySelectorAll('[data-ed="1"]'), function(el){
    el.addEventListener('blur', function(){ edCommit(el); });
    el.addEventListener('change', function(){ edCommit(el); });
    el.addEventListener('keydown', function(ev){
      if(ev.key === 'Enter'){ ev.preventDefault(); el.blur(); }
      else if(ev.key === 'Escape'){ el.value = el.dataset.orig || ''; el.blur(); }
    });
  });
}
function openDetail(d){
  /* 多选初值：库里是顿号连接的串 */
  MULTI_IVUNIT = String(d.interview_unit || '').split(/[、，,;；]+/)
    .map(function(x){ return x.trim(); }).filter(Boolean);
  var box = document.getElementById('detail');
  box.classList.remove('comparing');
  /* 换人 → 回到最上方（2026-10-28）。原来沿用上一个人的滚动位置：
     从第 1 个人（看到底）切到第 2 个人，右栏还停在半截，每次都得手动往上滚。
     用 data-cid 判断"是不是真的换人了"——面试字典异步到货后会再走一遍 openDetail，
     那次不该把人已经滚到的位置再拉回顶部。 */
  var switched = box.dataset.cid !== String(d.id);
  box.innerHTML = fullDetail(d);
  box.dataset.cid = String(d.id);
  if(switched) box.scrollTop = 0;
  multiRenderIvUnit();
  /* ⚠️ **绑定必须放在渲染之后、且每次渲染都要重绑**：
     面试字典是异步加载的，到货后会再渲染一次右栏 —— innerHTML 一换，
     之前绑在旧元素上的监听器（阶段下拉、档位按钮）**连同元素一起被丢掉**，
     表现就是"下拉能选但选了没反应"。 */
  bindDetail(d);
  hydrateAvatars(box);       /* 详情里的头像同样按可见性加载（见 fullDetail 的说明） */
  /* 淡灰可点击字段：点一下才变输入框（渲染后绑定） */
  edInit(box);
  /* 操作记录必须**每次渲染后都填一次**：面试字典是异步加载的，到货会再走一遍
     `openDetail`，`innerHTML` 一换，之前填好的审计内容就被重置回「正在读…」，
     而外面那个 `loadAudit` 只会在首次调用时跑一次（业务方 2026-10-21 实测卡在这里）。 */
  loadAudit(d.id);
  CUR_SNAP = formSnapshot(d);
  /* 换人了 → 右栏整体淡入一次（唯一一处"整块入场"）。
     不做逐段 stagger：那是装饰，不是反馈。 */
  box.classList.remove('swap'); void box.offsetWidth; box.classList.add('swap');
  killAutofill();
}
function fullDetail(d){
  var stage = d.stage || '';
  /* 详情页头像同样走懒加载（v3.0.3）：与列表用同一套 data-avatar 机制，
     避免"列表已改成懒加载、详情还是一次性请求"两套逻辑漂移。 */
  var av = d.avatar_path === '-' ? ''
    : '<img alt="" data-avatar="' + d.id + '"' +
      ' src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"' +
      ' onerror="this.remove()">';
  var row0 = ITEMS.filter(function(i){ return i.id === d.id; })[0] || {};
  var hits = row0.hits || [], miss = row0.miss || [];
  var apps = d.applications || [];
  var app0 = apps[0] || {};
  var missingFields = d.missing_fields || [];
  var parseFailed = !!d.parse_failed;
  var emailState = d.email_state || {};
  var extractMode = app0.extract_mode || '';
  var extractTxt = extractMode === 'llm+rule' ? '模型 + 规则'
    : (extractMode === 'heuristic' ? '规则通道（模型当时不可用，质量较低）'
    : (extractMode ? extractMode : '未记录'));
  var qualityHtml = '';
  if(parseFailed){
    qualityHtml = '<div class="quality bad"><b>简历原文读不出</b>　请打开下方原件人工核对；' +
      '不要只依据自动抽取字段做录用判断。</div>';
  }else if(missingFields.length){
    qualityHtml = '<div class="quality"><b>信息缺失：' + esc(missingFields.join('、')) +
      '</b>　可在本档案中人工更正；姓名、年限等缺失时请先核对原文。</div>';
  }

  /* 教育经历全部展开在姓名下方。第一学历与最高学历字段也留在这一区，
     避免同一项学校既在“基本信息”又在“教育经历”出现两份可编辑入口。 */
  /* 每一段都记下它在 `edu_history` 里的**原始下标**（`_hi`）：
     下面要支持"逐段改学校/专业"，而这里会过滤空段、还会补两条、最后再排序 ——
     渲染顺序早就不等于库内顺序了。带上下标才能改对那一段。
     补出来的两条（最高学历 / 第一学历）`_hi = -1`，不可按段编辑（它们不在 edu_history 里）。 */
  var segs = [];
  (d.edu_history || []).forEach(function(h, i){
    if(h && (h.school || '').trim()){
      var seg = {};
      for(var k in h){ if(Object.prototype.hasOwnProperty.call(h, k)) seg[k] = h[k]; }
      seg._hi = i;
      segs.push(seg);
    }
  });
  if(d.school && d.edu_level &&
     !segs.some(function(h){ return h.degree === d.edu_level && h.school === d.school; })){
    /* 补的这条也要带上**两个排名口径**（软科 + QS）与海外标记，
       否则最高学历学校不在 edu_history 里时，这一行会显示成"无排名" ——
       明明接口已经算出了 QS 名次却没人用（业务方 2026-10-21 实测踩到）。 */
    /* `_kind: 'top'` = 这条是"最高学历"补出来的，不在 edu_history 里，
       改它要落到主字段（school / major / edu_level）而不是分段 JSON。 */
    segs.push({degree: d.edu_level, school: d.school, major: d.major || '',
               tier: d.uni_tier, rank_no: d.uni_rank,
               qs_no: d.uni_qs, overseas: d.uni_overseas, _kind: 'top'});
  }
  /* 第一学历（本科）也要进**展示区**（业务方 2026-10-21）。
     上面是"这个人读过什么"，下面是"人工修正入口" —— 本科只出现在下面，
     会让人以为系统没识别到本科（实际是简历里本科段常不写起止时间，
     `edu_history` 抽不到那一条，而 `first_degree_school` 是另一条通道单独抽的）。
     层次/软科/QS 一并带上，否则这一行会显示成"无排名"。 */
  if((d.first_degree_school || '').trim() &&
     !segs.some(function(h){
       return h.degree === '本科' && h.school === d.first_degree_school;
     })){
    /* `_kind: 'bachelor'` = "第一学历"补出来的（筛选用独立字段，不在 edu_history 里） */
    segs.push({degree: '本科', school: d.first_degree_school, major: '',
               tier: d.bachelor_tier, rank_no: d.bachelor_rank,
               qs_no: d.bachelor_qs, overseas: d.bachelor_overseas, _kind: 'bachelor'});
  }
  var eduOrder = {'本科': 0, '硕士': 1, '研究生': 1, '博士': 2};
  segs.sort(function(a, b){
    var ai = eduOrder[a.degree] == null ? 9 : eduOrder[a.degree];
    var bi = eduOrder[b.degree] == null ? 9 : eduOrder[b.degree];
    return ai - bi;
  });
  var eduRows = segs.map(function(h){
    var period = ('');
    if(h.start || h.end){
      period = esc(h.start || '') + (h.end ? (' – ' + esc(h.end)) : '');
    }
    /* 排名标签：国内院校看**软科**，境外院校看 **QS**（两把尺子互不覆盖）。
       境外校在软科主榜里查不到，只写软科会让所有海归显示"无排名"；
       国内校则没有 QS 数据。所以按"有哪个显示哪个"，两个都有就都显示
       （清华北大同时上两个榜，两条信息都是真的，不该丢）。
       全都没有时区分一句：海外校说"无 QS 数据"（口径对得上），国内校说"无排名"。 */
    /* QS 用与 985/211 **同一个标签样式**（业务方 2026-10-21）：
       两者都是"这所学校什么水平"的判断依据，视觉权重一致才对；
       之前给 QS 单独做了中性底色，结果同一行里 QS 比 985 还弱，读起来像次要信息。 */
    var rankTag = (h.rank_no ? '<span class="rk">软科第 ' + h.rank_no + '</span>' : '') +
      (h.qs_no ? '<span class="chipl">QS 第 ' + h.qs_no + '</span>' : '') +
      ((!h.rank_no && !h.qs_no)
        ? (h.overseas ? '<span class="rk none">海外 · 无 QS 数据</span>'
                      : '<span class="rk none">无排名</span>')
        : '');
    var tags = (h.tier ? '<span class="chipl">' + esc(h.tier) + '</span>' : '') + rankTag;
    /* 每一段的**学历 / 学校 / 专业**都能就地改（业务方 2026-10-21）。
       三个字段的落点不同，交给 `editEduSeg` 按来源分派：
         `_hi >= 0`        → 改 edu_history 第 _hi 段
         `_kind === 'top'` → 改主字段（school / major / edu_level）
         `_kind === 'bachelor'` → 改 first_degree_school
       所以下面那排常驻修改框（第一学历/最高学历/学校/专业）已经不需要了。 */
    var kind = h._kind || '';
    var arg = (kind === 'bachelor' ? '\'bachelor\'' :
               kind === 'top' ? '\'top\'' : h._hi);
    var edit = function(cls, field, txt, tip, ph){
      return '<span class="' + cls + ' edu-click" title="' + tip + '" ' +
        'onclick="editEduSeg(' + arg + ',\'' + field + '\')">' +
        esc(txt || ph) + '</span>';
    };
    return '<div class="edu-line">' +
      (period ? '<span class="edu-period">' + period + '</span>' : '') +
      edit('edu-degree', 'degree', h.degree, '点击修改学历', '补学历') +
      edit('edu-school', 'school', h.school, '点击修改学校', '补学校') +
      edit('edu-major', 'major', h.major, '点击修改专业', '补专业') +
      (tags ? '<span class="edu-tags">' + tags + '</span>' : '') +
      '</div>';
  }).join('');
  var firstUnder = segs.filter(function(h){
    return h.degree === '本科' && (h.school || '').trim();
  })[0] || {};
  var firstDegreeValue = d.first_degree_school || firstUnder.school || '';
  var degreeOpts = ['', '大专', '本科', '硕士', '研究生', '博士'].map(function(v){
    return opt(v, d.edu_level, v || '学历未识别');
  }).join('');
  var eduBlock =
    '<div class="edu-band">' +
      '<div class="edu-band-head"><h4>教育经历</h4></div>' +
      '<div class="edu-list">' + (eduRows || '<div class="hint">未识别到教育经历</div>') + '</div>' +
      /* 这一排常驻修改框已移除（业务方 2026-10-21）：
         上面每一段的学历/学校/专业都能直接点开来改，
         再留四个输入框既重复、又把这里变成"待填的表单"。 */
    '</div>';
  /* 附件 */
  function docRows(list){
    return (list || []).map(function(x){
      var acts = x.archived_path
        ? '<button class="btn" onclick="openDoc(' + x.id + ',1)">在线打开</button>' +
          ' <button class="btn" onclick="openDoc(' + x.id + ',0)">下载</button>'
        : '<span class="hint">原件未留档</span>';
      return '<div class="doc"><span class="doc-n">' + esc(x.file_name || '') + '</span>' +
        '<span class="doc-a">' + acts + '</span></div>';
    }).join('') || '<div class="hint">无</div>';
  }

  /* 技能：命中 / 缺失 / 其余 */
  var skills = (d.skills || []).slice(0, 40);
  var rest = skills.filter(function(s){ return hits.indexOf(s.name) < 0; });
  var skillHtml = '';
  if(hits.length) skillHtml += '<div class="skill" style="margin-bottom:6px">' +
    hits.map(function(n){ return '<span class="hit">命中 ' + esc(n) + '</span>'; }).join('') + '</div>';
  if(miss.length) skillHtml += '<div class="skill" style="margin-bottom:6px">' +
    miss.map(function(n){ return '<span class="miss">缺 ' + esc(n) + '</span>'; }).join('') + '</div>';
  if(rest.length) skillHtml += '<div class="skill">' +
    rest.map(function(s){
      return '<span class="plain' + (s.verified ? '' : ' unverified') + '"' +
        (s.verified ? '' : ' title="岗位词表里有，但简历原文找不到证据"') + '>' +
        esc(s.name) + (s.verified ? '' : '（未核验）') + '</span>';
    }).join('') + '</div>';
  if(!skillHtml) skillHtml = '<div class="hint">没有抽到技能</div>';
  var evHtml = (d.skills || []).filter(function(s){ return s.evidence; }).slice(0, 3)
    .map(function(s){ return '<div class="ev"><b>' + esc(s.name) + '</b>：' + esc(s.evidence) + '</div>'; })
    .join('');

  /* 疑似重复档案（业务方 2026-10-21：原版有、v2 缺）。
     后端 `db.suspicious_duplicates` 随详情下发 `duplicates`，这里只渲染。
      **系统不自动合并** —— 同名可能是两个人（重名在招聘里很常见），
     所以只提示、只给 id，由 HR 判断。
     放在头部正下方：这是会影响"要不要继续推进"的信息，藏在页面底部等于没有。 */
  var dupWarn = '';
  if((d.duplicates || []).length && !dupDismissed(d.id)){
    /* 可关闭（业务方 2026-10-21）：同名同校在招聘里太常见，确认过不是同一个人之后
       这条提示不该一直占着版面。点「不是同一个人」后按候选人 id 记在本地，
       下次打开不再提示（只影响这一条，不动库里的数据与其它人的提示）。 */
    dupWarn = '<div class="dupwarn"><div class="dupwarn-t"><b>疑似重复档案</b>' +
        '（未自动合并，需你判断）：' +
        d.duplicates.map(function(x){
          return '<span class="dupi">#' + x.id + ' ' + esc(x.name || '') +
            (x.reason ? '（' + esc(x.reason) + '）' : '') + '</span>';
        }).join('') + '</div>' +
      '<button class="btn sm" onclick="dupDismiss(' + d.id + ')">不是同一个人</button>' +
    '</div>';
  }
  /* 敏感字段剔除明细：**只在真有剔除记录时才显示**（业务方 2026-10-21）。
     这块的作用是让人知道"模型看到的内容里，哪些隐私信息被提前拿掉了"——
     因为档位是模型判的，HR 有权知道它凭什么判、有没有因为缺信息而判偏。
     ⚠️ 但**没有明细时必须什么都不显示**：绝大多数档案是历史数据（当时没留痕），
     每条都弹一句"不代表没有敏感信息"，纯属噪音，看完反而以为系统在报错。 */
  var sens = d.sensitive_removed || {};
  var sensKeys = Object.keys(sens);
  var sensLine = sensKeys.length
    ? '<div class="sensline"><b>送模型前已剔除隐私信息：</b>' +
      sensKeys.map(function(k){
        return esc(k) + ' <b>' + esc(String(sens[k])) + '</b> 处';
      }).join('、') +
      '<div class="hint">剔除发生在调模型之前，原件本身不改动；' +
      '档位由模型根据剔除后的内容判断。</div></div>'
    : '';
  var ins = d.insight || {};
  var honors = d.honors || {};
  /* 通用扁平化：无论库里的元素是字符串、对象、还是对象里套数组，
     都取出一段可读文字。**绝不允许 `[object Object]` 出现在界面上** ——
     之所以加这层，是因为库里这类"荣誉"数据来自模型抽取，形状不受控
     （业务方 2026-10-21 反复看到 `[object Object]`，而当前库里 244 条又都是规范的，
     说明要么是旧页面、要么是某批数据形状不同；两种情况都得保证渲染不出错）。 */
  function flatHonor(v, depth){
    if(v == null) return '';
    if(typeof v === 'string') return v;
    if(typeof v === 'number') return String(v);
    if(depth > 3) return '';
    if(Array.isArray(v)){
      return v.map(function(x){ return flatHonor(x, depth + 1); })
        .filter(Boolean).join('、');
    }
    if(typeof v === 'object'){
      var pick = v.name || v.title || v.text || v.value || v.label || '';
      if(!pick){
        /* 没有常见字段名时，退而取它自己的第一个字符串值，总比 [object Object] 强 */
        for(var k2 in v){
          if(Object.prototype.hasOwnProperty.call(v, k2)){
            var f = flatHonor(v[k2], depth + 1);
            if(f) return f;
          }
        }
      }
      return flatHonor(pick, depth + 1);
    }
    return '';
  }
  var honorHtml = ['论文', '专利', '荣誉', '奖学金'].filter(function(k){
    return (honors[k] || []).length;
  }).map(function(k){
    var rows = (honors[k] || []).map(function(h){
      if(typeof h === 'string') return h;
      if(!h || typeof h !== 'object') return flatHonor(h, 0);
      var name = flatHonor(h.name || h.title || h.text || '', 0).trim();
      var evidence = flatHonor(h.evidence || h.detail || h.note || '', 0).trim();
      if(name && evidence && name !== evidence) return name + '：' + evidence;
      return evidence || name;
    }).filter(Boolean);
    return '<div class="ev"><b>' + esc(k) + '</b>：' + esc(rows.join('；')) + '</div>';
  }).join('') || '<div class="hint">无</div>';

  /* 投递来源（2026-10-28）：来源邮件 + 简历原件合成**一行**，放在档位上方。
     这两件事回答的是同一个问题——"这份档案从哪来的"，原来分两块沉在页面最底部，
     HR 判断"要不要推进"时得来回滚动才能对上是谁怎么投的。 */
  var srcMail = d.source_mail || {};
  var srcDocs = d.resume_documents || [];
  var srcDoc0 = srcDocs[0] || null;
  var srcRow = '<div class="srcbar">' +
    '<span class="srck">投递来源</span>' +
    '<span class="srcmail">' +
      (srcMail.subject
        ? '<span class="srcsub" title="' + esc(srcMail.subject) + '">' + esc(srcMail.subject) + '</span>' +
          (srcMail.from
            ? '<span class="srcmeta" title="' + esc(srcMail.from) + '">' + esc(srcMail.from) + '</span>' : '') +
          /* 日期去掉年份：一行里"09-21 21:28"够用了，标题比年份重要 */
          (srcMail.received_at
            ? '<span class="srcmeta">' + esc(String(srcMail.received_at).slice(5, 16)) + '</span>' : '')
        : '<span class="srcmeta">文件夹导入</span>') +
    '</span>' +
    (srcDoc0
      ? '<span class="srcfile">' +
          '<span class="fn" title="' + esc(srcDoc0.file_name || '') + '">' +
            esc(srcDoc0.file_name || '简历原件') + '</span>' +
          (srcDocs.length > 1 ? '<span class="srcmeta">共 ' + srcDocs.length + ' 份</span>' : '') +
          (srcDoc0.archived_path
            ? '<button class="btn" onclick="openDoc(' + srcDoc0.id + ',1)">在线打开</button>' +
              '<button class="btn" onclick="openDoc(' + srcDoc0.id + ',0)">下载</button>'
            : '<span class="srcmeta">未留档</span>') +
        '</span>'
      : '<span class="srcfile"><span class="srcmeta">无简历原件</span></span>') +
  '</div>';

  /* 档案操作条独立于个人信息：横跨右侧详情栏，滚动时固定在右上角。 */
  var actionBar = '<div class="detail-toolbar"><div class="dact">' +
    '<button class="btn" onclick="copyBasicInfo()" ' +
      'title="复制姓名 / 学历 / 性别 / 电话 / 邮箱 / 学校 / 专业为纯文本">复制信息</button>' +
    '<button class="btn primary" onclick="saveAll(' + d.id + ',' + (d.application_id || 0) + ')">保存更正</button>' +
    (d.application_id && stage === (STAGE_CFG.default_stage || '全部简历')
      ? '<button class="btn" id="btnAdv">推进初筛通过</button>' : '') +
    (d.application_id
      ? '<select id="stageSel" class="stagesel" title="推进阶段（写审计）">' +
        (STAGE_CFG.order || []).map(function(k){
          return '<option value="' + esc(k) + '"' + (k === stage ? ' selected' : '') + '>' +
            esc(k) + '</option>';
        }).join('') + '</select>' : '') +
    (d.application_id
      ? '<button class="btn" id="btnReview" data-undo="' + (d.reviewed ? '1' : '0') + '">'
        + (d.reviewed ? '撤销复核' : '标记已复核') + '</button>' : '') +
    '<button class="btn" onclick="reanalyze(' + d.id + ')">重新分析</button>' +
    '<button class="btn" onclick="archiveOne(' + d.id + ')">归档</button>' +
  '</div></div>';

  /* 操作条与个人信息完全分离：先铺右上操作条，再从下一行开始个人档案。 */
  return actionBar +
    '<div class="dhead profile-head">' +
    '<div class="av-lg">' + esc((d.name || '?').slice(0, 1)) + av + '</div>' +
    '<div class="dmain">' +
    '<div class="dname edu-click" title="点击修改姓名" ' +
      'onclick="editField(\'name\', ' + d.id + ')">' +
      esc(d.name || '未识别') + '</div>' +
    /* 顺序：**性别 → 岗位 → 状态 → 电话 → 邮箱**（业务方 2026-10-21：性别放第一个；
       标签全部收窄 —— 原来每个框都撑得很开，一行信息占了半屏还留大片空白）。
       四六级已挪到「荣誉 / 论文 / 专利」那一块：它是**资质**，不属于身份信息。 */
    '<div class="dmeta">' +
      '<span class="dfacts">' +
        edSel('edGender', [['', '性别'], ['男', '男'], ['女', '女']],
              (d.gender || '').trim(), 54) +
        '<i class="fdsep"></i>' +
        (app0.job_title
          ? '<button class="chipl jchip" title="点击修改岗位" ' +
              'onclick="openAssignJob(' + d.id + ')">' + esc(app0.job_title) + '</button>'
          : '<button class="chipl jchip none" title="点击指定岗位" ' +
              'onclick="openAssignJob(' + d.id + ')">未归岗 · 指定</button>') +
        ((d.status_display || app0.status_display)
          ? '<i class="fdsep"></i><span class="chipl">' +
              esc(d.status_display || app0.status_display) + '</span>' : '') +
      '</span>' +
      '<i class="vsep"></i>' +
      '<span class="dcontact">' +
        edText('edPhone', d.phone === '—' ? '' : d.phone, '电话', 136) +
        edText('edEmail', d.email === '—' ? '' : d.email, '邮箱', 208) +
        (emailState.sent
          ? '<span class="chipl" style="color:var(--ok)">已发邮件 ' +
            esc(String(emailState.count || 0)) + ' 次</span>' : '') +
      '</span>' +
    '</div>' +
    /* 第二行：**判断类标签**（阶段 / 结论 / 数据质量）。
       这些正是左侧列表里扫人时看的那几个标签，点进来却要往下翻到「面试与录用」
       才知道走到哪一步。放在联系方式下面那行正好原本是空的。 */
    (function(){
      var tags = '';
      if(d.application_id && stage){
        tags += '<span class="chipl stagechip" title="当前阶段">' + esc(stage) + '</span>';
      }
      tags += outcomeChip(d.outcome || '', stage);
      /* 录用函已发（系统内发出会自动记时间；线下发的可以手填）—— 有就挂个标，
         免得"到底发没发"要去翻审计。 */
      if(d.offer_sent_at){
        tags += '<span class="chipl offerchip" title="录用函发出时间">录用函 ' +
          esc(String(d.offer_sent_at).slice(5, 10).replace('-', '/')) + '</span>';
      }
      if(parseFailed){
        tags += '<span class="chipl flagbad" title="简历原文未能解析，需打开原件人工核对">' +
          '简历读不出</span>';
      }else if(missingFields.length){
        tags += '<span class="chipl flagwarn" title="没抽到：' + esc(missingFields.join('、')) +
          '">缺 ' + esc(missingFields.join('、')) + '</span>';
      }
      return tags ? '<div class="dmeta dmeta2">' + tags + '</div>' : '';
    })() +
    '</div>' +   /* .dmain 结束 */
    '</div>' +   /* .dhead 结束 */
    '<div class="profile-layout">' +
    dupWarn +
    qualityHtml +
    eduBlock +

    /* 荣誉/论文/专利紧跟教育经历（业务方 2026-10-21）：
       这几项都是"这个人在学校期间的产出"，跟学历背景是同一段经历的两面，
       分开放会被后面的判断区块隔断，读起来是两件事。 */
    /* 荣誉 / 论文 / 专利 + **英语四六级**（业务方 2026-10-21：四六级从头部挪到这里 ——
       它是"资质"，和荣誉/论文/专利是同一类东西；放在姓名下面那排身份信息里显得突兀）。
       标题也跟着改成实情，不再只写三项。 */
    '<div class="sec honosec profile-wide"><h3>荣誉 / 论文 / 专利 / 英语四六级</h3>' + honorHtml +
      '<div class="ev cetrow"><b>英语四六级</b>：' +
        edSel('edCetLv', [['', '未识别'], ['六级', '六级'], ['四级', '四级'],
                          ['专业八级', '专业八级'], ['专业四级', '专业四级'],
                          ['考研英语', '考研英语']], d.cet_level, 92) +
      '</div>' +
    '</div>' +

    /* 「基本信息」区块已删除（业务方 2026-10-21）：
       四六级/性别/电话/邮箱都挪到姓名下面那排（展示即可点改），姓名本身点标题就能改；
       再留一个区块等于同一批字段出现两次。复制按钮已挪到右上操作区。 */

    /* 投递来源一行（见上面 srcRow 的说明）：位置在档位正上方 ——
       HR 的阅读顺序是"这个人是谁 → 材料从哪来 → 怎么判的 → 走到哪一步了"。 */
    srcRow +

    /* ---- 档位（2026-10-21 挪位：从教育经历下方移到「面试与录用」正上方）----
       理由：档位是"这个人的判断结论"，面试与录用是"这个人的过程事实"，
       两者挨在一起看才是完整判断链；放在教育经历下面反而被夹在中间。 */
    '<div class="sec tier-sec" style="margin-top:16px"><h3>档位</h3>' +
      '<div class="bar2">' + tierSeg(d) + '</div></div>' +

    /* 更多操作放在档位正下方（业务方 2026-10-21）。
       这几个动作（面试提纲 / 指定岗位 / 证件照）都是"我接下来要对这个人做什么"，
       和档位（"该不该推进"）是同一类判断，挨着放比沉在页面最底部顺手得多。 */
    '<div class="sec"><h3>更多操作</h3>' +
      '<div class="bar2">' +
        '<button class="btn" onclick="getOutline(' + d.id + ')">面试提纲</button>' +
        '<button class="btn" onclick="adoptSuggestedJob(' + d.id + ')">采纳建议岗位</button>' +
        '<button class="btn" onclick="openAssignJob(' + d.id + ')">指定岗位</button>' +
        '<button class="btn" onclick="avatarRefresh(' + d.id + ')">证件照重新抽取</button>' +
        '<button class="btn" onclick="avatarClear(' + d.id + ')">去掉证件照</button>' +
      '</div>' +
      '<div id="mOutline" style="margin-top:8px"></div>' +
    '</div>' +

    /* ---- 面试与录用（可编辑）----
   按语义分三组，各带底色与组标题（业务方 2026-10-21：原来十几个控件堆两行，
   日期/时段/方式连标签都没有，根本看不出是干什么的）。
   ⚠️ 字段 id 一个都不能改 —— `formSnapshot`/`formDiff` 按 id 取值，
   「保存更正」靠它们判断"哪几项被改过"。 */
    /* 面试与录用（2026-10-28 重排）：**一行排完**。
       原来拆成「面试安排 / 面试结论」左右两栏、三个组标题、控件 12px 字，
       面试单位的输入框窄到放不下三个单位名。现在是一整行：单位占主要宽度，
       其余四项按内容宽度跟随；标签 13.5px、控件 34px 高。
       ⚠️ 字段 id 一个都不能改 —— `formSnapshot`/`formDiff` 按 id 取值，
       「保存更正」靠它们判断"哪几项被改过"。 */
    '<div class="sec profile-wide ivsec"><h3>面试与录用' +
      (d.application_id ? '' : '（还没有投递记录）') + '</h3>' +
      '<div class="ivrow">' +
        '<span class="fld fld-wide"><span class="fl">面试单位</span>' +
          '<div class="multi" id="multi_ivunit"></div>' +
          '<input type="hidden" id="ivUnit" value="' + esc(d.interview_unit || '') + '">' +
        '</span>' +
        '<span class="fld"><span class="fl">面试日期</span>' +
          '<input id="ivDate" type="date" value="' +
            esc(String(d.interview_date || '').slice(0, 10)) + '"></span>' +
        '<span class="fld"><span class="fl">时段</span>' +
          '<input id="ivSlot" list="dlIvSlot" placeholder="09:30-10:30" ' +
            'value="' + esc(d.interview_slot || '') + '"></span>' +
        '<datalist id="dlIvSlot">' + (IDICT.slots || []).map(function(x){
          return '<option value="' + esc(x) + '"></option>'; }).join('') + '</datalist>' +
        '<span class="fld"><span class="fl">方式</span>' +
          '<input id="ivMode" list="dlIvMode" placeholder="现场面试" ' +
            'value="' + esc(d.interview_mode || '') + '"></span>' +
        '<datalist id="dlIvMode">' + (IDICT.modes || []).map(function(x){
          return '<option value="' + esc(x) + '"></option>'; }).join('') + '</datalist>' +
        '<span class="fld"><span class="fl">结论</span>' + outcomeField(d) + '</span>' +
      '</div>' +
      /* 录用与入职：第二行，与面试事实之间一条细线 —— 它是"面试之后"的事。 */
      '<div class="ivrow">' +
        '<span class="fld"><span class="fl">入职情况</span>' +
          '<select id="obStatus">' +
            ['', '未入职', '已入职', '放弃'].map(function(v){
              return opt(v, d.onboard_status, v || '未填'); }).join('') +
          '</select></span>' +
        '<span class="fld"><span class="fl">录用函发出</span>' +
          '<input id="obOffer" type="datetime-local" value="' +
            esc(String(d.offer_sent_at || '').replace(' ', 'T').slice(0, 16)) + '"></span>' +
        '<span class="fld"><span class="fl">签约</span>' +
          '<input id="obSigned" type="date" value="' +
            esc(String(d.signed_at || '').slice(0, 10)) + '"></span>' +
        '<span class="fld"><span class="fl">入职期限</span>' +
          '<input id="obDeadline" type="date" value="' +
            esc(String(d.onboard_deadline || '').slice(0, 10)) + '"></span>' +
      '</div>' +
    '</div>' +

    /* ---- 备注 ---- */
    '<div class="sec"><h3>备注</h3>' +
      '<textarea id="edNote" rows="3" placeholder="沟通印象、内推关系、谈薪情况……">' +
        esc(d.note || '') + '</textarea>' +
    '</div>' +

    /* 流程事实 → 判断结论 → 能力证据 → 原始材料 → 审计，按 HR 的阅读顺序排列。 */
    '<div class="sec"><h3>投递记录（' + apps.length + '）</h3>' +
      (apps.length ? apps.map(function(a){
        return '<div class="doc"><span class="doc-n">#' + a.id + '　' +
          esc(a.job_title || '待指定') + '　' + esc(a.stage || '—') + '　' +
          esc(String(a.applied_at || '').slice(0, 16)) + '</span>' +
          '<span class="doc-a"><span class="chipl gray">' +
          esc(a.tier_final || a.tier_suggested || '—') + '</span></span></div>';
      }).join('') : '<div class="hint">无</div>') +
    '</div>' +

    /* 系统分析与技能属于**证据**，不是**结论**：整页只有档位是"用力"的地方，
       这两块压小一号（`quiet`），否则每块都在喊，等于没人喊。 */
    '<div class="sec quiet"><h3>系统分析</h3>' +
      (ins.summary ? '<div class="sum">' + esc(ins.summary) + '</div>'
                   : '<div class="hint">还没有分析结论</div>') +
      ((ins.reasons || []).length ? '<div class="ev">依据：' + esc(ins.reasons.join('；')) + '</div>' : '') +
      ((ins.risks || []).length ? '<div class="ev">风险：' + esc(ins.risks.join('；')) + '</div>' : '') +
      /* 隐私剔除明细挪到这里：它解释的正是"模型凭什么判"，跟着分析结论走才对得上 */
      sensLine +
    '</div>' +

    '<div class="sec quiet"><h3>技能</h3>' +
      skillHtml + evHtml + '</div>' +

    /* ---- 原文与抽取质量：HR 核对字段时必须能看到模型/规则读到的原始材料 ---- */
    '<div class="sec profile-wide"><h3>简历原文与抽取质量</h3>' +
      '<div class="ev">字段抽取方式：<b>' + esc(extractTxt) + '</b>' +
        (app0.confidence != null ? '　置信度 ' + esc(String(app0.confidence)) : '') +
      '</div>' +
      (emailState.sent
        ? '<div class="hint">已发送邮件 ' + esc(String(emailState.count || 0)) + ' 次' +
          (emailState.last_at ? '，最近 ' + esc(String(emailState.last_at).replace('T', ' ').slice(0, 16)) : '') +
          (emailState.last_subject ? '，主题「' + esc(emailState.last_subject) + '」' : '') + '</div>'
        : '') +
      '<details class="rawbox"><summary>展开简历原文' +
        (d.raw_text ? '（' + esc(String(d.raw_text.length)) + ' 字）' : '') +
      '</summary><pre>' + esc(d.raw_text || '（无文本，需人工查看附件原件；原件已留档）') +
      '</pre></details>' +
    '</div>' +

    /* 简历原件已上移到「投递来源」那一行（见 srcRow）；
       这里只剩"同一封邮件里的其他附件"，多数档案没有，所以保持安静。 */
    ((d.other_documents || []).length
      ? '<div class="sec quiet"><h3>其他附件（' + d.other_documents.length + '）</h3>' +
        docRows(d.other_documents) + '</div>' : '') +
    /* 操作记录（2026-10-20 补齐；2026-10-21 去掉「档位来源」—— 业务方只要操作记录）。
       审计调 `/api/audit` 按候选人过滤，v2 之前**零调用** —— 谁在什么时候改了
       档位/阶段/复核，界面上查不到，HR 不敢在生产上用。异步拉，不阻塞档案渲染。 */
    '<div class="sec profile-wide"><h3>操作记录</h3>' +
      '<div id="auditBox" class="hint">正在读操作记录…</div>' +
    '</div>' +

    '</div>';
}

/* 改某一段教育经历的学校 / 专业（业务方 2026-10-21）。
   `hi` 是该段在 `edu_history` 里的**原始下标**；改完重画档案。
   若改的正好是最高学历那一段，后端会同步更新 `school` / `major` 主字段
   （档位、院校层次、排名都读主字段，不同步会出现"看着改了、档位没动"）。 */
/* 改单个主字段（目前只用于姓名 —— 它是大字标题，不适合做成淡灰可点框）。
   其余字段（四六级/性别/电话/邮箱）走 `edText/edSel` 那套，参与「保存更正」。 */
async function editField(field, cid){
  var d = CUR_DETAIL || {};
  var cur = (field === 'name') ? (d.name || '') : '';
  var v = await askText('修改姓名', cur, '1-30 个汉字或字母');
  if(v === null || v === true) return;
  v = String(v).trim();
  if(!v || v === cur) return;
  var body = {};
  body[field] = v;
  var r = await api('/api/candidates/' + cid + '/rename', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if(r.__err){ toast(r.__err, 'danger'); return; }
  toast('已更新姓名', 'ok');
  load(); if(CUR) pick(CUR);
}

async function editEduSeg(which, field){
  var d = CUR_DETAIL || {};
  var isTop = (which === 'top'), isBach = (which === 'bachelor');
  var seg = (isTop || isBach) ? null : (d.edu_history || [])[which];
  if(!isTop && !isBach && !seg){ toast('这一段不在可编辑范围内', 'warn'); return; }

  /* 当前值：补位那两条分别取主字段 */
  var cur = isTop
    ? (field === 'degree' ? (d.edu_level || '')
       : field === 'school' ? (d.school || '') : (d.major || ''))
    : isBach
      ? (field === 'school' ? (d.first_degree_school || '') : '')
      : (seg[field] || '');

  var titles = {degree: '修改学历', school: '修改学校', major: '修改专业'};
  var phs = {degree: '本科 / 硕士 / 博士', school: '学校全名',
             major: '专业名'};
  var v = await askText(titles[field] + (seg && seg.degree ? '（' + seg.degree + '）' : ''),
                        cur, phs[field]);
  if(v === null || v === true) return;
  v = String(v).trim();
  if(v === cur) return;

  /* 学历只允许这四个值 —— 写错会导致档位判定按"未知学历"处理 */
  if(field === 'degree' && v && ['大专', '本科', '硕士', '博士'].indexOf(v) < 0){
    toast('学历只能填：大专 / 本科 / 硕士 / 博士', 'warn');
    return;
  }

  var r;
  if(isTop || isBach){
    /* 补位条目不在一段段 JSON 里，走 `/rename` 改主字段：
       最高学历 → school / major / education；第一学历 → first_degree_school */
    var body = {};
    if(isBach){
      body.first_degree_school = v;
    }else{
      body[field === 'degree' ? 'education' : field] = v;
    }
    r = await api('/api/candidates/' + d.id + '/rename', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  }else{
    var body2 = {index: which};
    body2[field] = v;
    r = await api('/api/candidates/' + d.id + '/edu-history', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body2)});
  }
  if(r.__err){ toast(r.__err, 'danger'); return; }
  toast(r.note || '已更新', 'ok');
  load(); if(CUR) pick(CUR);
}

/* 「疑似重复」已确认：按候选人 id 记在本地，之后不再提示这一条。
   只存在浏览器里（不动库、不写审计）—— 它是提醒不是数据，HR 自己心里有数即可；
   换台机器会再提示一次，这个代价可以接受（比误合并两个人便宜得多）。 */
function dupDismissed(cid){
  try{ return localStorage.getItem('hragent.dupok.' + cid) === '1'; }
  catch(e){ return false; }
}
function dupDismiss(cid){
  try{ localStorage.setItem('hragent.dupok.' + cid, '1'); }catch(e){}
  var el = document.querySelector('#detail .dupwarn');
  if(el) el.style.display = 'none';
  toast('已记录，之后不再提示这条', 'ok');
}

function bindDetail(d){
  /* 面试结论：**改完立即生效**（原版就是这样，不进「保存更正」）。
     放进批量保存会出现"改两次"的问题。 */
  var oc = document.getElementById('ivOutcome');
  if(oc){
    oc.addEventListener('change', function(){
      var v = oc.value;
      if(!d.application_id){ toast('这条投递还没有编号，先补录岗位'); return; }
      setOutcome(d.application_id, d.stage, v);
    });
  }
  var sel = document.getElementById('stageSel');
  if(sel){
    sel.addEventListener('change', function(){
      var v = sel.value;
      if(!d.application_id || v === d.stage) return;
      setStage(d.application_id, v);
    });
  }
  var seg = document.querySelector('.seg');
  if(seg){
    Array.prototype.forEach.call(seg.querySelectorAll('button'), function(b){
      b.addEventListener('click', function(){
        if(!d.application_id){ toast('这条投递还没记录，无法改档'); return; }
        setTier(d.application_id, b.dataset.tier);
      });
    });
  }
  /* 「推进初筛通过」：新收简历的默认动作（点一下就到初筛通过）。
   目标阶段取配置里的第二个（初筛通过），不写死字符串。 */
var adv = document.getElementById('btnAdv');
if(adv){
  adv.addEventListener('click', function(){
    adv.disabled = true;
    setStage(d.application_id, (STAGE_CFG.order || [])[1] || '初筛通过');
  });
}
var rv = document.getElementById('btnReview');
  if(rv){
    rv.addEventListener('click', function(){
      setReview(d.application_id, rv.dataset.undo === '1');
    });
  }
}

/* ---------- 动作 ---------- */
async function setTier(aid, tier){
  var r = await api('/api/applications/' + aid + '/tier',
    {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({tier: tier})});
  if(r.__err){ toast(r.__err); return; }
  await load(); if(CUR) await pick(CUR);
  pulse(document.querySelector('.seg button.on'));
  toast('档位已改为 ' + tier);
}
/* 操作记录（2026-10-20 补齐，2026-10-21 修正数据源）。
   为什么必须有：改档、改阶段、复核、改字段都写审计，但 v2 里**零调用**，
   于是"谁在什么时候改了什么"只能去翻数据库 —— HR 不敢在生产上用它做决定。

   ⚠️ **数据源必须用 `audit_snippet`，不能自己查 `/api/audit?entity=candidate`**：
   改档、改阶段、合并这类动作记在 `entity=application`，发信记 `entity=mail`，
   附件记 `entity=document` —— 只查 candidate 会漏掉它们，
   表现就是"刚改完阶段，档案里却说暂无操作记录"。
   后端的 `db.candidate_audit` 正是把四类实体合并成一条时间线，
   并且**已经随详情下发**（`server.py:1139` 的 `audit_snippet`），
   直接用即可，不用再发一次请求。 */
function renderAuditItems(el, items){
  if(!el) return;
  if(!items.length){ el.textContent = '暂无操作记录。'; return; }
  /* 按原版的口径（`ui.py:2495-2503`）用**表格**呈现，而不是技术化的字段列表。
     只显示 `readable`（"改档位：A → B"这种一句中文，已经含动作）；
     不要再拼 `action_label` —— 那是同一句话的前半段，拼起来会变成
     "修改字段 修改字段：备注=旧 → 新"，重复且难读（业务方 2026-10-21 反馈）。 */
  /* 折叠呈现 + 醒目的折叠头（业务方 2026-10-21：直接铺表格「不太明显」）。 */
  el.innerHTML =
    '<details class="auditbox">' +
      '<summary><span class="audit-arrow">▸</span>查看操作记录' +
        '<span class="audit-n">' + items.length + ' 条</span></summary>' +
      '<table>' +
        '<thead><tr><th>时间</th><th>做了什么</th><th>操作人</th></tr></thead>' +
        '<tbody>' + items.map(function(x){
          return '<tr>' +
            '<td class="t">' + esc(String(x.ts || '').slice(0, 16)) + '</td>' +
            '<td>' + esc(String(x.readable || x.action_label || '').slice(0, 200)) + '</td>' +
            '<td>' + esc(x.operator || '') + '</td></tr>';
        }).join('') + '</tbody></table>' +
      '<div id="auditNote" class="hint" style="margin-top:6px">只看改动了数据的动作；'
        + '查看、下载等只读操作不记。</div>' +
    '</details>';
}
function loadAudit(cid){
  var box = document.getElementById('auditBox');
  if(!box) return;
  var d = CUR_DETAIL || {};
  /* 详情里已经带回来了（含改档/改阶段/发信），直接用，省一次请求 */
  if(String(d.id) === String(cid) && (d.audit_snippet || []).length){
    renderAuditItems(box, d.audit_snippet);
    return;
  }
  if(String(d.id) === String(cid) && d.audit_snippet){
    renderAuditItems(box, []);      // 详情明确给了空数组 = 确实没有记录
    return;
  }
  api('/api/audit?entity=candidate&entity_id=' + encodeURIComponent(cid) + '&limit=30')
    .then(function(r){
      if(MODE !== 'detail' || CUR !== cid) return;      // 已切走就别写进别人的框
      var el = document.getElementById('auditBox');
      if(!el) return;
      if(r.__err){ el.textContent = '操作记录读取失败：' + r.__err; return; }
      renderAuditItems(el, r.items || []);
    });
}
/* ---------- 专业 / 技能本体联想（2026-10-20 补齐） ----------
   原版有 `loadMajorList`（`/api/majors`）与 `loadSkillList`（`/api/ontology`），
   给 JD 的「专业要求 / 必需技能 / 加分技能」提供**规范名候选**——
   输入几个字就有提示，避免同一个人被写成"材料成型"与"材料成形"两种。
   v2 之前这两个端点**从不请求**（纯自由输入），口径就漂了。 */
function loadOntologyHints(){
  if(ONTOLOGY_DONE) return;
  ONTOLOGY_DONE = true;
  api('/api/majors?limit=400').then(function(r){
    if(r && !r.__http_error && r.items && r.items.length){
      ONTOLOGY_MAJORS = r.items.map(function(m){ return m.name; }).filter(Boolean);
    }
  }).catch(function(){});
  api('/api/ontology').then(function(r){
    if(r && !r.__http_error && r.skills && r.skills.length){
      ONTOLOGY_SKILLS = r.skills.map(function(s){ return s.canonical; }).filter(Boolean);
    }
  }).catch(function(){});
}
var ONTOLOGY_DONE = false;
var ONTOLOGY_MAJORS = [];
var ONTOLOGY_SKILLS = [];
/* 挂在已有的 `<datalist>` 上；没有就现场建一个（挂在 body 上即可全局生效） */
function attachHint(inputId, values){
  var el = document.getElementById(inputId);
  if(!el || !values || !values.length) return;
  var dl = document.getElementById('dl_' + inputId);
  if(!dl){
    dl = document.createElement('datalist');
    dl.id = 'dl_' + inputId;
    document.body.appendChild(dl);
  }
  dl.innerHTML = values.map(function(v){ return '<option value="' + esc(v) + '"></option>'; }).join('');
  el.setAttribute('list', dl.id);
}
async function setReview(aid, undo){
  if(!aid){ toast('这条投递还没有编号', 'warn'); return; }
  var r = await api('/api/applications/' + aid + '/review', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({undo: !!undo})});
  if(r.__err){ toast(r.__err, 'danger'); return; }
  toast(r.note || (undo ? '已撤销复核' : '已复核'), 'ok');
  await load(); if(CUR) await pick(CUR);
}
async function setStage(aid, stage){
  /* 手工推进到「已发放offer」时**先确认一次**（2026-10-28）。
     在系统里发录用函会自动写「录用函发出时间」并推进阶段；但如果录用函是
     线下发的（纸质/别的邮箱），HR 只能手动改阶段 —— 这时档案上没有任何
     录用函记录，而"录用函发送时间"是后面所有时限的起算点，漏了会影响后续。
     不拦死，只让人确认一次；确认后照常推进并留审计。 */
  var cur = CUR_DETAIL || {};
  if(stage === '已发放offer' && !cur.offer_sent_at){
    var ok = await askConfirm({
      title: '还没有录用函记录',
      body: '这个人在系统里没有「录用函发出时间」。\n\n' +
            '如果是线下发出的（纸质、或用别的邮箱发的），继续推进后请在本档案的' +
            '「面试与录用 → 录用函发出」里补上日期。\n\n仍要推进到「已发放offer」吗？',
      okText: '继续推进', cancelText: '先去补记录'});
    if(!ok) return;
  }
  var r = await api('/api/applications/' + aid + '/stage',
    {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({stage: stage})});
  if(r.__err){ toast(r.__err); return; }
  await load(); if(CUR) await pick(CUR);
  pulse(document.querySelector('.dact .btn'));
  toast(r.note || ('已推进到「' + stage + '」'));
}
/* ---------- 导入 / 导出 / 批量（原版这些入口在筛选区上方，v2 收到第二行） ---------- */
/* 管道超期天数：在管道旁直接设置（原版口径：停留多少天算超期 → 管道里标红） */
async function setPipeOverdue(){
  var r = await api('/api/settings');
  var cur = r.pipeline_overdue_days || 15;
  var v = await askText('超期设置', String(cur), '停留多少天算超期（管道里超过就标红）');
  if(v === null || v === true) return;
  var d = parseInt(v, 10);
  if(!d || d < 1){ toast('请填 1 以上的天数'); return; }
  var r2 = await api('/api/settings', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({pipeline_overdue_days: d})});
  toast(r2.__err ? ('保存失败：' + r2.__err) : ('超期设为 ' + d + ' 天'));
  loadPipe();
}

/* ---------- 面试字典：条目可**增删**（原版有"添加/删除"） ----------
   原来只能整体编辑那四个文本框 —— 改一个错别字要把整段重打。
   这里给每个维度加一条"条目行"：每条一个 × 可删，下面一个输入框可加。 */
function dictItemsRow(key, label, items){
  var list = items || [];
  return '<div class="crow"><label>' + esc(label) + '</label><div class="cinput dictrow">' +
    (list.length ? list.map(function(v, i){
      return '<span class="mchip">' + esc(v) +
        '<b onclick="dictDel(\'' + key + '\',' + i + ')" title="删除这条">×</b></span>';
    }).join('') : '<span class="hint">（还没有条目）</span>') +
    '<input id="dictAdd_' + key + '" placeholder="输入后回车添加">' +
  '</div></div>';
}
async function dictAdd(key){
  var inp = document.getElementById('dictAdd_' + key);
  if(!inp) return;
  var v = (inp.value || '').trim();
  if(!v){ toast('先输入内容'); return; }
  var ta = document.getElementById(key === 'contacts' ? 'cedv_cfgContacts' : 'cedv_cfg' +
      ({units:'Units', rooms:'Rooms', slots:'Slots', modes:'Modes'}[key] || 'Units'));
  if(!ta){ toast('找不到对应的编辑框'); return; }
  var lines = ta.value.split('\n').map(function(s){ return s.trim(); }).filter(Boolean);
  if(lines.indexOf(v) >= 0){ toast('已经在列表里了'); return; }
  lines.push(v);
  ta.value = lines.join('\n');
  inp.value = '';
  await sysAct('dict');
  initSys();
}
async function dictDel(key, idx){
  var ta = document.getElementById(key === 'contacts' ? 'cedv_cfgContacts' : 'cedv_cfg' +
      ({units:'Units', rooms:'Rooms', slots:'Slots', modes:'Modes'}[key] || 'Units'));
  if(!ta) return;
  var lines = ta.value.split('\n').map(function(s){ return s.trim(); }).filter(Boolean);
  if(idx < 0 || idx >= lines.length) return;
  lines.splice(idx, 1);
  ta.value = lines.join('\n');
  await sysAct('dict');
  initSys();
}

/* ---------- 导入/收取：**进度管理模块**（原版移植） ----------
   ⚠️ `/api/ingest` 是**立即返回 task_id** 的（不是同步返回结果），
   真正的进度与报表在 `/api/tasks/{task_id}` 里轮询。
   v2 之前把它当最终结果读 → 提示永远是"新增 0"，而且收取在后台跑着没人知道。
   现在按原版两阶段走：
     ① 读取阶段：报总数 + 实时进度 + 可「优雅停止」（读完当前文件才停）
     ② 读取完成后**同一个弹窗原地切换**为逐条分析进度（不关不弹、不闪）
     ③ 最后给一次完整的结果弹窗
   另外：页面刷新过也能挂回去（查 /api/tasks/active），绝不重复开新任务
   （重复收取会并发抢同一个邮件游标、重复烧模型额度）。 */
var __progTimer = null;
var __ingestTask = null;   /* {taskId, label, source, phase:'fetch'|'insight'} */
var __ingesting = false;

function ensureDlgClose(){
  var t = document.getElementById('dlgTitle');
  if(!t) return;
  var x = t.querySelector('.dlg-x');
  if(!x){
    x = document.createElement('b');
    x.className = 'dlg-x';
    x.textContent = '×';
    t.appendChild(x);
  }
  x.title = '关闭';
  x.onclick = function(){ closeProgDlg(); };
  var m = document.getElementById('mask');
  if(m){
    m.onclick = function(ev){
      if(ev.target === ev.currentTarget) closeProgDlg();
    };
  }
}
function openProgDlg(title, html){
  var t = document.getElementById('dlgTitle');
  t.textContent = title;
  var b = document.getElementById('edDlgBody');
  b.style.display = 'block';
  b.innerHTML = html;
  document.getElementById('dlgBody').style.display = 'none';
  document.getElementById('dlgNo').style.display = 'none';
  document.getElementById('dlgYes').style.display = 'none';
  var mask = document.getElementById('mask');
  mask.classList.add('on');
  ensureDlgClose();
}
function closeProgDlg(){
  if(__progTimer){ clearInterval(__progTimer); __progTimer = null; }
  /* 宽体样式是"这一屏专属"的，关窗必须摘掉，否则下一个普通弹窗也会被撑成 940px */
  var wide = document.querySelector('#mask .dlg.wide');
  if(wide) wide.classList.remove('wide');
  var b = document.getElementById('edDlgBody');
  if(b){ b.style.display = 'none'; b.innerHTML = ''; }
  document.getElementById('dlgBody').style.display = '';
  document.getElementById('dlgNo').style.display = '';
  document.getElementById('dlgYes').style.display = '';
  var bar = document.querySelector('#mask .dbar');
  if(bar) bar.style.display = '';
  var mask = document.getElementById('mask');
  mask.classList.remove('on');
  mask.onclick = null;
  var title = document.getElementById('dlgTitle');
  var x = title ? title.querySelector('.dlg-x') : null;
  if(x) x.remove();
}
function resultModal(opts){
  var o = opts || {};
  var kind = o.kind || 'info';
  var color = kind === 'ok' ? 'var(--ok)'
            : (kind === 'danger' ? 'var(--bad)'
            : (kind === 'warn' ? 'var(--warn)' : 'var(--accent)'));
  openProgDlg(o.title || '完成',
    '<div style="line-height:1.8;white-space:pre-wrap">' +
      '<div style="font-size:15px;color:' + color + ';font-weight:600;margin-bottom:8px">' +
        esc(o.headline || '') + '</div>' +
      '<div>' + (o.body || '') + '</div>' +
    '</div>' +
    '<div class="bar" style="margin-top:18px;justify-content:flex-end">' +
      '<button class="btn primary" onclick="closeProgDlg()">' + esc(o.okText || '知道了') + '</button></div>');
}
/* 居中进度弹窗：真实进度条 + 可停止 */
function progressModal(taskId, opts){
  var o = opts || {};
  if(!taskId){ resultModal({title: o.title || '处理中', body: o.body || '', kind: 'info'}); return; }
  if(__progTimer){ clearInterval(__progTimer); __progTimer = null; }
  openProgDlg(o.title || '处理中…',
    '<div>' +
      '<div style="font-size:14px;color:var(--ink-2);margin-bottom:4px">' +
        esc(o.desc || '正在处理，请稍候') + '</div>' +
      '<div class="progtrack"><div class="progfill" id="progFill" style="width:0%"></div></div>' +
      '<div class="progmeta" id="progMeta">准备中…</div>' +
      (o.stoppable
        ? '<div class="bar" style="margin-top:14px;justify-content:flex-end">' +
          '<button class="btn" id="progStopBtn" onclick="progressStop(\'' + esc(taskId) + '\')" ' +
          'title="读完正在处理的这一份就停下；已处理的全部保留，下次从停下的地方继续">' +
          '停止（读完当前文件后停下）</button></div>'
        : '') +
    '</div>');
  var fill = document.getElementById('progFill');
  var meta = document.getElementById('progMeta');
  var stopped = false;
  var tick = async function(){
    if(stopped) return;
    var r = await api('/api/tasks/' + taskId);
    /* await 期间可能已经换到下一阶段了（收取完成 → 接力到逐条分析），
       这时旧轮询必须**立刻退出**，不能再往下走触发旧阶段的 onDone。 */
    if(stopped) return;
    if(r.__err || !r.ok){
      stopped = true;
      clearInterval(__progTimer); __progTimer = null;
      var msg = (r && (r.__err || r.error)) || '任务不存在或已过期';
      if(o.onDone){
        o.onDone({status: 'error', message: msg,
                  result: {ok: false, error: msg}});
      }else{
        resultModal({title: '任务已中断', kind: 'danger',
                     headline: '无法继续读取任务',
                     body: esc(msg)});
      }
      return;
    }
    var pct = r.percent || 0;
    if(fill) fill.style.width = pct + '%';
    var line = r.total ? ('已完成 ' + (r.done || 0) + ' / ' + r.total +
                          (r.failed ? '，失败 ' + r.failed : '')) : '';
    if(meta){
      meta.textContent = (r.status === 'done')
        ? (r.message || line)
        : (r.message ? (line ? (line + ' · ' + r.message) : r.message)
                     : (line || '正在统计数量…'));
    }
    if(r.status === 'done' || r.status === 'error'){
      stopped = true;
      clearInterval(__progTimer); __progTimer = null;
      var sb = document.getElementById('progStopBtn');
      if(sb) sb.disabled = true;
      if(o.chain){ if(o.onDone) o.onDone(r); return; }
      var okAll = !r.failed;
      resultModal({
        title: r.status === 'error' ? '处理中断' : (okAll ? '全部完成' : '有失败'),
        kind: r.status === 'error' ? 'danger' : (okAll ? 'ok' : 'warn'),
        headline: r.status === 'error' ? '任务出错' : (okAll ? '处理成功' : '部分失败'),
        body: esc(r.message || '') +
              '<div style="margin-top:6px;color:var(--ink-2)">共 ' + (r.total || 0) +
              ' 人：完成 ' + (r.done || 0) + ' 人' +
              (r.failed ? '，失败 ' + r.failed + ' 人' : '') + '</div>'
      });
      if(o.onDone) o.onDone(r);
    }
  };
  /* ⚠️ **顺序不能反**（2026-10-21 修复）：必须**先起定时器、再首轮 tick**。
     反过来写（先 tick 后 setInterval）时，tick 里的 await 一旦在
     「任务完成 → 回调里接力到下一阶段」之后才返回，它就会把**新一轮**的
     `__progTimer` 覆盖回旧的那一个 —— 旧轮询因此复活，继续用**旧阶段的** onDone
     触发（收取阶段完成后，分析阶段没有 report，统计就全成了 0）。
     这正是「明明收取到了，却显示新增 0」的真正原因。 */
  __progTimer = setInterval(tick, 1200);
  tick();
}
async function progressStop(taskId){
  var r = await api('/api/tasks/' + taskId + '/stop', {method:'POST'});
  var sb = document.getElementById('progStopBtn');
  if(sb){ sb.disabled = true; sb.textContent = '已请求停止（读完当前文件后停下）'; }
  toast(r.__err ? ('停止失败：' + r.__err) : '已请求停止');
}
function setIngestBusy(on, source){
  ['btnIngestMail', 'btnIngestDir'].forEach(function(id){
    var b = document.getElementById(id);
    if(!b) return;
    if(on){
      b.dataset.old = b.dataset.old || b.textContent;
      /* **不禁用** —— 任务在跑的时候点它要能重新打开进度窗
         （原版口径：弹窗被误关后，这是唯一能回到"停止"按钮的路）。 */
      b.disabled = false;
      b.classList.add('busy');
      b.textContent = '收取中…（点此看进度）';
    }else if(b.dataset.old){
      b.disabled = false;
      b.classList.remove('busy');
      b.textContent = b.dataset.old;
    }
  });
}
/* 重新打开进度窗（弹窗被误关后唯一的回来路径） */
function ingestReopen(){
  if(!__ingestTask || !__ingestTask.taskId) return;
  var t = __ingestTask;
  /* ⚠️ **必须按阶段分开**（2026-10-21 修复，照原版 `ui.py:2863-2879` 补齐）：
     v2 原来两个阶段完成都调 `ingestFetchDone`，而它只从**任务进度**里取 report ——
     分析阶段的进度里没有 report，于是收取阶段的统计全变成 0
     （业务方看到「明明收取到了，却显示新增 0、新版本 0…」）。
     正确口径：
       · 读取阶段完成 → 取任务里的 report（收取统计），若带 insight_task_id 就接力；
       · 分析阶段完成 → 用**上一阶段存下的 rep**（t.rep）+ 分析结果（ar）出最终弹窗。
     另外两阶段都传 chain:true —— chain 的意思是"完成时不弹通用结果窗，
     由 onDone 自己弹"，否则会先弹一个「全部完成」再弹「邮箱完成」，弹两次。 */
  if(t.phase === 'insight'){
    var q = (t.rep || {}).insight_queued;
    progressModal(t.taskId, {
      title: '入库后自动分析',
      desc: '正在对新增的 ' + (q == null ? 0 : q) + ' 人做模型分析',
      chain: true,
      onDone: function(ar){ ingestFinal(t.label || '导入', t.rep || {}, ar); }
    });
    return;
  }
  progressModal(t.taskId, {
    title: (t.label || '导入') + '读取中',
    desc: '正在读取…（读取完成后会自动接力逐条分析）',
    stoppable: true, chain: true,
    onDone: function(fr){ ingestFetchDone(t.label || '导入', fr); }
  });
}
/* 挂回服务器上还在跑的任务（页面刷新过的情况） */
function ingestAttach(task){
  if(!task || !task.task_id) return;
  __ingestTask = {taskId: task.task_id, label: '导入', source: null, phase: 'fetch'};
  __ingesting = true;
  setIngestBusy(true);
  ingestReopen();
}
/* 读取阶段结束（正常/停止/出错都到这里） */
function ingestFetchDone(label, fr){
  setIngestBusy(false);
  /* ⚠️ **两阶段接力**（2026-10-21 修复）：收取阶段完成后，report 里带
     `insight_task_id`，界面原地切到「逐条分析」再轮询一次。
     但**第二次回来的 r 是分析任务的进度，它没有 report** ——
     收取阶段的统计（新增/跳过重复/无附件/…）如果不在这里带下来，
     弹窗里就全是 0：业务方看到的是「明明收取到了，却显示新增 0、新版本 0…」。
     所以：把上一阶段存下来的 rep 与本阶段的合并。
     ⚠️ 合并后 rep 里仍带着 insight_task_id，接力判断必须加 phase 守卫，
     否则会「分析完成 → 又去接力 → 永远出不来」。 */
  var prev = (__ingestTask && __ingestTask.rep) || {};
  var rep = Object.assign({}, prev, (fr && fr.report) || {});
  if(fr && fr.status === 'error'){
    __ingestTask = null; __ingesting = false;
    resultModal({title: label + '导入失败', kind: 'danger', headline: '任务出错',
                 body: esc(fr.message || '收取失败')});
    return;
  }
  var phase = (__ingestTask && __ingestTask.phase) || 'fetch';
  if(rep.insight_task_id && phase !== 'insight'){
    /* 分析阶段接力：同一个弹窗原地切换，不关不弹、不闪 */
    __ingestTask = {taskId: rep.insight_task_id, label: label,
                    source: (__ingestTask || {}).source || null, phase: 'insight', rep: rep};
    ingestReopen();
    return;
  }
  ingestFinal(label, rep, fr);
}
function ingestFinal(label, rep, ar){
  __ingestTask = null; __ingesting = false;
  setIngestBusy(false);
  load();
  var num = function(k){ return (rep[k] == null ? 0 : rep[k]); };
  var summary = '新增 ' + num('added') + '，新版本 ' + num('merged_versions') +
    '，跳过重复 ' + num('skipped_dup') + '，无附件 ' + num('no_attachment') +
    '，解析失败(仍入库) ' + num('parse_failed') + '，异常 ' + num('failed');
  var extra = num('insight_skipped') > 0
    ? '<div style="margin-top:8px;color:var(--ink-2)">有 ' + num('insight_skipped') +
      ' 人未分析（"入库即分析"当前是关的）——可点「分析待分析的人」批量补上。</div>' : '';
  var stopped = rep.stopped
    ? '<div style="margin-top:8px;color:var(--warn)">已按你的要求停止（完成当前文件后停下）；' +
      '已读取的全部保留，下次收取会从停下的地方继续。</div>' : '';
  /* 逐条分析阶段的结果（ar = 分析任务的进度）。之前这个参数**接了但没用**，
     于是"入库即分析"跑完了也不告诉用户结果 —— 现在补上。 */
  var ana = '';
  if(ar && (ar.done || ar.failed || ar.message)){
    var aLine = ar.message ? ar.message
      : ('完成 ' + (ar.done || 0) + ' 人' + (ar.failed ? ('，失败 ' + ar.failed + ' 人') : ''));
    ana = '<div style="margin-top:8px;color:var(--ink-2)">逐条分析：' + esc(aLine) + '</div>';
  }
  resultModal({title: label + '完成', kind: num('failed') ? 'warn' : 'ok',
               headline: num('failed') ? '部分失败' : '收取完成',
               body: summary + stopped + extra + ana});
}
async function doIngest(source){
  var label = source === 'mailbox' ? '邮箱' : '本地文件夹';
  /* 已在跑（含"收取中…（点此看进度）"那个状态）：**重新弹出进度窗口**，
     而不是把人挡在门外 —— 这是误关弹窗后唯一能回到"停止"按钮的路。 */
  if(__ingesting){
    if(__ingestTask && __ingestTask.taskId){
      toast('任务还在进行中，已重新打开进度窗口');
      ingestReopen();
    }else{
      toast('已有一个导入任务在进行中，请等它完成');
    }
    return;
  }
  /* 页面刷新过（本页没记住任务号）：先问服务器有没有在跑的，有就挂回去 */
  try{
    var act = await api('/api/tasks/active');
    if(act && act.ok && act.task){
      toast('检测到还在进行中的任务，已重新打开进度窗口');
      ingestAttach(act.task);
      return;
    }
  }catch(e){ /* 查询失败按"没有任务"处理 */ }
  setIngestBusy(true, source);
  var r;
  try{
    r = await api('/api/ingest', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({source: source})});
  }catch(e){
    setIngestBusy(false);
    resultModal({title: label + '导入失败', kind: 'danger', body: '请求失败：' + (e && e.message || e)});
    return;
  }
  if(r.__err){
    setIngestBusy(false);
    resultModal({title: label + '导入失败', kind: 'danger', body: esc(r.__err)});
    return;
  }
  __ingesting = source;
  __ingestTask = {taskId: r.task_id, label: label, source: source, phase: 'fetch'};
  ingestReopen();
}

async function doUpload(files){
  var arr = Array.prototype.filter.call(files || [], function(f){
    return /[.](jpe?g|png|bmp|webp|pdf|docx?|txt)$/i.test(f.name);
  });
  if(!arr.length){
    resultModal({title:'上传', kind:'warn', body:'没有可上传的文件（支持图片 / PDF / Word / txt）'});
    return;
  }
  /* 给出可数的进度：多份文件的裸字节上传常常十几秒，只弹一个 toast 之后界面就没反馈了*/
  setModalTitle('上传简历文件');
  document.getElementById('edDlgBody').innerHTML =
    '<div>' +
      '<div style="font-size:14px;color:var(--ink-2);margin-bottom:4px">正在上传 ' +
        arr.length + ' 份文件</div>' +
      '<div class="progtrack"><div class="progfill" id="upFill" style="width:0%"></div></div>' +
      '<div class="progmeta" id="upMeta">准备中…</div>' +
    '</div>' +
    '<div class="bar" style="margin-top:18px;justify-content:flex-end">' +
      '<button class="btn" onclick="closeModal()">关掉（后台继续传）</button></div>';
  openEditorDlg();
  var fill = document.getElementById('upFill');
  var meta = document.getElementById('upMeta');
  var ok = 0, fail = 0, failedNames = [];
  for(var i = 0; i < arr.length; i++){
    var f = arr[i];
    if(meta) meta.textContent = '第 ' + (i + 1) + ' / ' + arr.length + ' 份：' + f.name;
    if(fill) fill.style.width = Math.round(i / arr.length * 100) + '%';
    try{
      /* 裸字节上传，文件名走 query。
         ⚠️ **不能用 multipart**（2026-10-20 修复：原来发 FormData，后端直接 422，
         而界面还照常提示"上传完成：新增 0"，属静默假成功）。
         后端 api_upload 刻意不引 python-multipart —— 打包产物里没有它，
         而这条路径恰恰是离线兜底，引入依赖会让"能不能跑"取决于打包有没有收进去，
         见 `server.py:565-573` 的注释。一个文件一个请求，循环即可。 */
      var r = await fetch(withToken('/api/upload?name=' + encodeURIComponent(f.name)),
        {method:'POST', headers: hdr({'Content-Type':'application/octet-stream'}), body:f});
      var j = await r.json().catch(function(){ return {}; });
      if(r.ok && !j.error){ ok++; }
      else{
        fail++;
        /* 后端的 detail 有两种形状：字符串（`raise HTTPException(detail="…")`，
           如"文件超过 20MB"）或 Pydantic 的数组（`[{loc,msg,type}]`）。
           原来直接 `det[0]` —— 字符串取下标得到的是**第一个字**，
           于是失败明细显示成「简历.pdf：文」，等于没说原因。 */
        var det = j.detail, why = '';
        if(typeof det === 'string') why = det;
        else if(det && det.length) why = (det[0] && det[0].msg) || String(det[0]);
        failedNames.push(f.name + '：' + (j.error || why || ('HTTP ' + r.status)));
      }
    }catch(e){ fail++; failedNames.push(f.name + '：网络错误'); }
  }
  if(fill) fill.style.width = '100%';
  resultModal({
    title: '上传完成',
    kind: fail ? (ok ? 'warn' : 'danger') : 'ok',
    headline: fail ? (ok ? '部分文件未上传成功' : '全部文件上传失败') : '上传成功',
    body: '共 ' + arr.length + ' 份：成功 ' + ok + ' 份'
      + (fail ? ('，失败 ' + fail + ' 份') : '，全部已入库')
      + (failedNames.length
          ? '<div style="margin-top:10px;color:var(--bad);font-size:13px">失败明细：<br>'
            + failedNames.map(function(n){ return esc(n); }).join('<br>') + '</div>'
          : '')
  });
  var inp = document.getElementById('upFile');
  if(inp) inp.value = '';
  await load();
}
function saveBlob(blob, name){
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function(){ URL.revokeObjectURL(a.href); }, 4000);
}
async function doExport(){
  toast('正在导出名单…');
  var r = await fetch(withToken('/api/export/roster' + query()), {headers: hdr({})});
  if(!r.ok){ toast('导出失败 HTTP ' + r.status); return; }
  saveBlob(await r.blob(), '候选人名单.xlsx');
  toast('已导出');
}
async function doBundle(){
  toast('正在打包初筛简历…');
  var r = await fetch(withToken('/api/bundle/initial-pass'),
    {method:'POST', headers:hdr({})});
  if(!r.ok){ var j = await r.json().catch(function(){ return {}; });
    toast(j.detail || ('打包失败 HTTP ' + r.status)); return; }
  saveBlob(await r.blob(), '初筛通过.zip');
  toast('已打包');
}
function setAll(on){
  Object.keys(ITEMS.reduce(function(m, x){ m[x.id] = 1; return m; }, {})).forEach(function(k){
    SEL[k] = on;
  });
  Array.prototype.forEach.call(document.querySelectorAll('.chk input'), function(c){
    c.checked = on;
  });
  syncSel();
}
function syncSel(){
  var n = Object.keys(SEL).filter(function(k){ return SEL[k]; }).length;
  var b = document.getElementById('btnArch');
  b.textContent = '归档勾选（' + n + '）';
  b.disabled = n === 0;
  var c = document.getElementById('btnCompare');
  if(c){
    c.textContent = '对比档案（' + n + '/' + COMPARE_MAX + '）';
    c.disabled = n === 0;
    c.title = n > 5 ? '最多固定 5 个档案，请减少勾选' : '选择 2-5 人后打开固定档案标签';
  }
}
async function archiveChecked(){
  var ids = Object.keys(SEL).filter(function(k){ return SEL[k]; }).map(Number);
  if(!ids.length) return;
  var okA = await askConfirm({title: '归档确认',
    body: '把勾选的 ' + ids.length + ' 人归档？\n归档是软归档，随时可以在「归档」页恢复。',
    okText: '归档'});
  if(!okA) return;
  var r = await api('/api/candidates/archive-batch', {method:'POST',
    /* ⚠️ 参数名必须是 `ids`（不是 candidate_ids）—— 写错时后端收到空列表，
       **静默什么都不做**，界面只看到"点了没反应"。本轮实测抓到的真 bug。 */
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({ids: ids, archived: true})});
  if(r.__err){ toast(r.__err); return; }
  SEL = {}; syncSel();
  var chk = document.getElementById('chkAll');
  if(chk) chk.checked = false;
  toast('已归档 ' + (r.changed || ids.length) + ' 人' +
        (r.skipped ? '（跳过 ' + r.skipped + ' 人）' : '') +
        (r.ended_applications ? ('，并结束 ' + r.ended_applications + ' 条进行中的投递') : ''));
  await load();
  document.getElementById('detail').innerHTML = '<div class="hint">左侧选一个人</div>';
  CUR = null;
  /* 归档的自然落点就是「归档」页 —— 归档完直接带过去看结果 */
  setView('archive');
}
async function reanalyze(cid){
  toast('重新分析中，约 10 秒…');
  var r = await api('/api/candidates/' + cid + '/reanalyze', {method:'POST'});
  if(r.__err){ toast(r.__err); return; }
  toast(r.ok ? '已重新分析' : (r.error || '分析未完成'));
  await load(); await pick(cid);
}
async function archiveOne(cid){
  var okB = await askConfirm({title: '归档确认',
    body: '把这个人归档？\n随时可以在「归档」页恢复。', okText: '归档'});
  if(!okB) return;
  var r = await api('/api/candidates/' + cid + '/archive', {method:'POST'});
  if(r.__err){ toast(r.__err); return; }
  toast('已归档');
  CUR = null;
  document.getElementById('detail').innerHTML = '<div class="hint">左侧选一个人</div>';
  await load();
}
/* ---------- 表单快照与差异保存（与旧版同一套口径）---------- */
var CUR_SNAP = {};
/*: 字段 → 接口分组。顺序即提交顺序；`key` 是后端认的字段名。 */
var FORM_FIELDS = [
  ['基本信息','edName','name'], ['基本信息','edEdu','education'],
  ['基本信息','edSchool','school'], ['基本信息','edMajor','major'],
  ['基本信息','edFD','first_degree_school'], ['基本信息','edGender','gender'],
  ['基本信息','edPhone','phone'], ['基本信息','edEmail','email'],
  ['基本信息','edCetLv','cet'],
  ['面试与录用','ivUnit','interview_unit'], ['面试与录用','ivDate','interview_date'],
  ['面试与录用','ivSlot','interview_slot'], ['面试与录用','ivMode','interview_mode'],
  ['面试与录用','obOffer','offer_sent_at'], ['面试与录用','obSigned','signed_at'],
  ['面试与录用','obDeadline','onboard_deadline'], ['面试与录用','obStatus','onboard_status'],
];
function formSnapshot(){
  var m = {};
  FORM_FIELDS.forEach(function(f){
    var el = document.getElementById(f[1]);
    m[f[1]] = el ? String(el.value == null ? '' : el.value).trim() : '';
  });
  /* 面试单位是多选：存的是顿号连接的串，读取也取 hidden 的值 */
  m.ivUnit = MULTI_IVUNIT.join('、');
  m.edNote = (document.getElementById('edNote') || {}).value || '';
  return m;
}
function formDiff(){
  var out = {基本信息:{}, 面试与录用:{}, 备注:null};
  FORM_FIELDS.forEach(function(f){
    var el = document.getElementById(f[1]);
    if(!el) return;
    var now = String(el.value == null ? '' : el.value).trim();
    if(now !== String(CUR_SNAP[f[1]] == null ? '' : CUR_SNAP[f[1]]).trim())
      out[f[0]][f[2]] = now;
  });
  var note = (document.getElementById('edNote') || {}).value || '';
  if(note !== (CUR_SNAP.edNote || '')) out.备注 = note;
  return out;
}
/* 一次保存整张档案改过的部分：基本信息 / 面试与录用 / 备注。
   分三次请求是因为后端本来就是三个职责不同的接口；在这里编排比造一个"什么都管"的接口稳。
   **没改的字段不提交**（避免把没填的项清成 NULL）。 */
async function saveAll(cid, aid){
  var diff = formDiff();
  var nB = Object.keys(diff.基本信息).length;
  var nF = Object.keys(diff.面试与录用).length;
  var nN = diff.备注 === null ? 0 : 1;
  if(!nB && !nF && !nN){ toast('没有改动任何内容'); return; }
  var parts = [];
  if(nB){
    var r = await api('/api/candidates/' + cid + '/rename',
      {method:'POST', headers:{'Content-Type':'application/json'},
       body: JSON.stringify(diff.基本信息)});
    if(r.__err){ toast(r.__err); return; }
    parts.push('基本信息 ' + (r.changed || []).length + ' 项');
  }
  if(nF){
    if(!aid){ toast('这条投递还没有编号，面试与录用信息没保存'); return; }
    var r2 = await api('/api/applications/' + aid + '/interview-info',
      {method:'POST', headers:{'Content-Type':'application/json'},
       body: JSON.stringify(diff.面试与录用)});
    if(r2.__err){ toast(r2.__err); return; }
    parts.push('面试与录用 ' + (r2.changed || []).length + ' 项');
    if(r2.advanced_to) parts.push('阶段已自动推进到「' + r2.advanced_to + '」');
  }
  if(nN){
    var r3 = await api('/api/candidates/' + cid + '/note',
      {method:'POST', headers:{'Content-Type':'application/json'},
       body: JSON.stringify({note: diff.备注})});
    if(r3.__err){ toast(r3.__err); return; }
    parts.push('备注');
  }
  toast('已保存：' + parts.join(' · '));
  await load(); await pick(cid);
}
/* 附件：在线打开 / 下载 */
function openDoc(id, inline){
  var url = '/api/documents/' + id + '/file' + (inline ? '?inline=1' : '');
  if(inline) window.open(withToken(url), '_blank');
  else{
    var a = document.createElement('a');
    a.href = withToken(url); a.download = ''; document.body.appendChild(a); a.click(); a.remove();
  }
}
/* 让"刚才那个动作"有可见反馈：重新播一次关键帧（先强制回流，否则连续两次不重播） */
function pulse(el){
  if(!el) return;
  el.classList.remove('pulse');
  void el.offsetWidth;
  el.classList.add('pulse');
}

/* ---------- 登录（v3.0.3） ---------- */
/* 设计取舍：**不做"登录页"与"应用页"两个 URL**。
   单文件内联页面多一个路由就要多处理一次缓存/刷新/回退，而登录只需要
   一件事：在拿到 token 之前把人挡在外面。
   这里用一层遮罩 + 401 自动召回：任何接口返回 401 都把遮罩弹回来，
   同时清掉失效 token —— 会话过期、被管理员踢下线都能正确回到登录页。 */
var LOGIN_SHOWN = false;
/* 退场动画（v3.2）跑完之前不再弹回来：登录成功后 bootAfterLogin() 会连发几个
   请求，其中任何一个 401 都会把登录页叫回来 —— 那会盖在"正在进去"的动画上。 */
var LOGIN_LEAVING = false;
function showLogin(msg){
  var w = document.getElementById('loginWrap');
  if(!w || LOGIN_LEAVING) return;
  LOGIN_SHOWN = true;
  /* 从 hidden 打开时重播入场。**只在这一刻播**：401 把登录页叫回来时
     w.hidden 本来就是 false，这里不会加 enter，用户不必再等一次淡入。 */
  if(w.hidden) w.classList.add('enter');
  w.hidden = false;
  var e = document.getElementById('loginErr');
  if(e){
    e.textContent = msg || '';
    /* 先摘再挂，强制重排，动画才会重播（连续两次输错口令时第二次也得抖） */
    e.classList.remove('on');
    if(msg){ void e.offsetWidth; e.classList.add('on'); }
  }
  var u = document.getElementById('loginUser');
  if(u && !u.value){ try{ u.focus(); }catch(_e){} }
  loadLoginHint();
  startLoginNet();
}
/* 失败反馈（v3.2）：框横向轻抖一下。"口令不对"这件事不用读字就该知道；
   连续错两次、三次也照样抖 —— 所以每次先摘类再强制重排。 */
function loginShake(){
  var box = document.querySelector('#loginWrap .loginbox');
  if(!box) return;
  box.classList.remove('shake');
  void box.offsetWidth;
  box.classList.add('shake');
}
/* 首次部署时把默认账号写在登录页上（v3.1.3）。
   原来登录页只有"忘记口令请联系管理员" —— 而管理员自己也不知道初始口令，
   于是双击打开、看到登录框、输不进去，判定为"程序坏了"。
   这条提示只在**口令仍是默认值**时出现，改过之后自动消失。 */
var LOGIN_HINT_LOADED = false;
async function loadLoginHint(){
  if(LOGIN_HINT_LOADED) return;
  LOGIN_HINT_LOADED = true;
  try{
    var r = await fetch('/api/login-hint');
    if(!r.ok) return;
    var j = await r.json();
    if(!j || !j.first_run || !j.hint) return;
    var box = document.getElementById('loginFirstHint');
    if(box){ box.hidden = false; box.textContent = j.hint; }
    var u = document.getElementById('loginUser');
    if(u && !u.value && j.username){ u.value = j.username; }
    var p = document.getElementById('loginPass');
    if(p && !p.value){ try{ p.focus(); }catch(_e){} }
  }catch(e){ /* 拿不到提示不影响登录 */ }
}
function hideLogin(){
  var w = document.getElementById('loginWrap');
  if(w) w.hidden = true;
  if(w) w.classList.remove('enter','leaving');
  var box = document.querySelector('#loginWrap .loginbox');
  if(box) box.classList.remove('shake','leaving');
  setLoginBtn('idle');                 /* 下次被 401 弹回来时按钮是干净的 */
  LOGIN_SHOWN = false;
  LOGIN_LEAVING = false;
  stopLoginNet();
}
/* 登录成功后的退场（v3.2）：**三段同时起步**，不是串行等待 ——
     ① 登录框放大 4% 并淡出（像镜头从它身上推开）
     ② 整层遮罩淡出（露出下面的应用区）
     ③ 应用区从 98.5% 展开到 100%（body.locked 由 bootAfterLogin 摘掉）
   合计 280ms。之后才真正 hidden —— `[hidden]{display:none}` 会立刻掐断过渡，
   所以**不能**在动画开始时就设 hidden，这正是原来"页面闪一下"的原因。 */
function loginExit(done){
  var w = document.getElementById('loginWrap');
  var box = document.querySelector('#loginWrap .loginbox');
  if(!w || !box || !LOGIN_SHOWN){ if(done) done(); return; }
  LOGIN_LEAVING = true;
  stopLoginNet();
  box.classList.add('leaving');
  w.classList.add('leaving');
  setTimeout(function(){
    hideLogin();
    if(done) done();
  }, 290);
}
function setToken(t){
  TOKEN = t || '';
  try{ if(TOKEN) localStorage.setItem('tp_token', TOKEN); else localStorage.removeItem('tp_token'); }
  catch(e){ /* 隐私模式下 localStorage 可能不可写，忽略 */ }
}
/* 按钮的三个状态（v3.2）：idle → busy（转环）→ done（对勾）/ 回 idle。
   文字只换一次（"登录"↔"登录中…"），图标在旁边换形状；
   按钮宽高固定，内容变化不会让它跳动。 */
function setLoginBtn(state){
  var btn = document.getElementById('loginBtn');
  var txt = document.getElementById('loginBtnText');
  if(!btn) return;
  btn.classList.toggle('busy', state === 'busy');
  btn.classList.toggle('done', state === 'done');
  btn.disabled = (state === 'busy');
  if(txt) txt.textContent = state === 'busy' ? '登录中…'
                             : state === 'done' ? '登录成功' : '登录';
}
/* 口令错误：抖动 + 错误行淡入。**不再用 alert 式的纯文字**——
   文字保留（读屏要靠它），但加了一层"不用读也知道"的动作反馈。 */
function loginFail(msg){
  var err = document.getElementById('loginErr');
  if(err){
    err.textContent = msg || '登录失败';
    err.classList.remove('on');
    void err.offsetWidth;
    err.classList.add('on');
  }
  loginShake();
}
async function doLogin(user, pass){
  var err = document.getElementById('loginErr');
  if(err) err.textContent = '';
  setLoginBtn('busy');
  try{
    /* 登录本身**不能**走 api()：那时还没有 token，而 api() 见到 401 会再弹登录页。 */
    var r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: user, password: pass})
    });
    var raw = await r.text();
    var j = {};
    try{ j = raw ? JSON.parse(raw) : {}; }catch(e){ j = {detail: raw}; }
    if(!r.ok){
      setLoginBtn('idle');
      loginFail(j.detail || ('登录失败（HTTP ' + r.status + '）'));
      return false;
    }
    setToken(j.token);
    /* 对勾先画出来（220ms），让人看见"成了"，再退场。这 220ms 不是浪费：
       退场本身要 280ms，两段重叠后总时长仍是 ~300ms，观感却完整得多。 */
    setLoginBtn('done');
    await new Promise(function(res){ setTimeout(res, 220); });
    var exited = new Promise(function(res){ loginExit(res); });
    await bootAfterLogin();
    await exited;
    return true;
  }catch(ex){
    setLoginBtn('idle');
    loginFail('网络错误：' + String(ex));
    return false;
  }
}
/* 登录成功后把界面真正拉起来。**单独一个函数**：因为登录态下这些请求
   第一次都会带 401，必须在拿到 token 之后才发。 */
async function bootAfterLogin(){
  if(AUTH_ENABLED) await loadMe();
  renderWhoami();
  /* renderWhoami() → applyPermissions() 在这里摘掉 body.locked，
     应用区于是从 98.5% 淡入展开 —— 和登录层的淡出是同时的。 */
  await load();
  await showOverview();
}
async function loadMe(){
  var j = await api('/api/me');
  if(j && !j.__err){ ME = j; }
}
function renderWhoami(){
  var box = document.getElementById('whoami');
  if(box){
    if(!AUTH_ENABLED || !ME || !ME.username){ box.innerHTML = ''; }
    else{
      box.innerHTML = '<b>' + esc(ME.display_name || ME.username) + '</b>' +
        '<span>' + esc(ME.role_label || '') + '</span>' +
        '<button type="button" onclick="doLogout()">退出</button>';
    }
  }
  applyPermissions();
}
/* 按权限显示/隐藏界面元素（v3.0.3）。
   **只负责藏，不负责防**：真正的拦截在服务端 `require()`。
   藏起来是为了让只读用户看不到一堆点了会 403 的按钮。 */
function can(perm){
  if(!AUTH_ENABLED) return true;             /* 本机模式：不限制 */
  return !!(ME && (ME.permissions || []).indexOf(perm) >= 0);
}
function applyPermissions(){
  var nav = document.getElementById('navUsers');
  if(nav) nav.hidden = !can('manage_users');
  /* 只读角色（没有 set_stage）不该看到任何改动入口 */
  var readonly = AUTH_ENABLED && !can('set_stage');
  document.body.classList.toggle('readonly', readonly);
  /* 系统配置页对没有 settings 权限的人也不该出现 */
  Array.prototype.forEach.call(document.querySelectorAll('.side button'), function(b){
    if(b.dataset.view === 'sys') b.hidden = !can('settings');
  });
  /* 没登录时**整个应用区不可见**（v3.0.3）：
     登录页是全屏遮罩，但底下的 DOM 仍在 —— 搜索引擎/截图/读屏都能看到
     "读取概览…" 这类内容，看起来像坏了。用 body 类整体压暗隐藏更干净。 */
  /* 已经摘掉过就不再重复设置：每次 applyPermissions() 都 toggle 一遍，会触发
     一次多余的重算（v3.2 起这一行还挂着 transition，写回同值虽不产生动画，
     但没必要每渲染一次就碰一次）。 */
  var locked = AUTH_ENABLED && !ME;
  if(document.body.classList.contains('locked') !== locked){
    document.body.classList.toggle('locked', locked);
  }
}
async function doLogout(){
  try{ await fetch('/api/logout', {method:'POST', headers: hdr()}); }catch(e){}
  setToken('');
  ME = null;
  location.reload();
}

/* ---------- 人才星图（v3.2） ----------
   一张"点与点连成网"的 canvas：60 个 2px 蓝点缓慢漂移，距离 130px 以内的点
   自动连线，鼠标 170px 内的连线变亮、点被轻轻吸引。**语义上它就是这个产品**：
   人才库、关系网；不是随手贴的科技感背景。

   三条硬约束（决定了它长这样）：
     1. **位置在白色蒙版之下**：CSS 里 canvas 是 z-index:0、登录框 z-index:1，
        而 .loginwrap 自己的背景层（照片 + 白色渐变蒙版）在 canvas 之下。
        所以粒子是被蒙版压淡的，永远抢不走登录框的注意力。
     2. **只跑登录页**：登录成功、页面切走、窗口最小化时立刻停（见 stopLoginNet
        与 visibilitychange）—— 这类动画最大的问题是"关掉界面还在烧 CPU"。
     3. **尊重 prefers-reduced-motion**：只画一帧静态网，不跑逐帧循环。

   为什么用 canvas 不用 DOM：60 个点 + 每帧最多几百条线，DOM 方案要维护上千个
   元素并每帧改样式；办公机（集显/远程桌面）上 DOM 必掉帧，canvas 一帧一次绘制。 */
var NET = { raf:0, pts:[], ctx:null, cvs:null, w:0, h:0, mx:-9999, my:-9999,
            t:0, on:false, bound:false };
var NET_REDUCED = false;
try{ NET_REDUCED = !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches); }catch(e){}

function netSize(){
  var c = NET.cvs;
  if(!c) return;
  var dpr = Math.min(window.devicePixelRatio || 1, 2);   /* 上限 2：4K 屏上 3x 太费 */
  var r = c.getBoundingClientRect();
  NET.w = Math.max(1, Math.round(r.width));
  NET.h = Math.max(1, Math.round(r.height));
  c.width  = Math.round(NET.w * dpr);
  c.height = Math.round(NET.h * dpr);
  var ctx = c.getContext('2d');
  if(ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  NET.ctx = ctx;
}
function netSeed(){
  /* 点数按面积走：小窗口里点会挤成一团，大屏上又太稀。
     分母 16000 ≈ 1920×1080 得 130 个，够密；上限 130 防止 4K 屏上算力失控。 */
  var n = Math.max(34, Math.min(130, Math.round(NET.w * NET.h / 16000)));
  var pts = [];
  for(var i = 0; i < n; i++){
    pts.push({
      x: Math.random() * NET.w, y: Math.random() * NET.h,
      /* 速度 ±0.15px/帧：约 9px/秒。再快就成了"飘雪"，再慢就看不出在动。 */
      vx: (Math.random() - 0.5) * 0.3, vy: (Math.random() - 0.5) * 0.3,
      r: 1 + Math.random() * 1.2
    });
  }
  NET.pts = pts;
}
function netFrame(){
  var ctx = NET.ctx;
  if(!ctx) return;
  var w = NET.w, h = NET.h, pts = NET.pts, i, j, a, b, dx, dy, d, d2;
  ctx.clearRect(0, 0, w, h);
  var LINK = 130, LINK2 = LINK * LINK, MOUSE = 170, MOUSE2 = MOUSE * MOUSE;

  if(!NET_REDUCED){
    for(i = 0; i < pts.length; i++){
      a = pts[i];
      a.x += a.vx; a.y += a.vy;
      /* 撞边反弹而不是绕回：绕回会在边缘出现"凭空冒出来一个点"的跳变 */
      if(a.x < 0){ a.x = 0; a.vx = -a.vx; } else if(a.x > w){ a.x = w; a.vx = -a.vx; }
      if(a.y < 0){ a.y = 0; a.vy = -a.vy; } else if(a.y > h){ a.y = h; a.vy = -a.vy; }
      /* 鼠标吸引：只给一点点力（0.004），像被轻轻带一下，不是被吸过去 */
      dx = NET.mx - a.x; dy = NET.my - a.y;
      d2 = dx * dx + dy * dy;
      if(d2 < MOUSE2 && d2 > 1){
        d = Math.sqrt(d2);
        a.vx += (dx / d) * 0.004; a.vy += (dy / d) * 0.004;
      }
      /* 限速：吸引累积久了会越飞越快 */
      a.vx = Math.max(-0.45, Math.min(0.45, a.vx));
      a.vy = Math.max(-0.45, Math.min(0.45, a.vy));
    }
  }

  /* 连线：先画普通网，再画鼠标附近的亮线（两遍是为了让亮线压在普通线上面） */
  ctx.lineWidth = 0.6;
  ctx.beginPath();                 /* 同色同宽的线合成一条路径，一次 stroke —— 逐条 stroke 慢 5 倍以上 */
  for(i = 0; i < pts.length; i++){
    a = pts[i];
    for(j = i + 1; j < pts.length; j++){
      b = pts[j];
      dx = a.x - b.x; dy = a.y - b.y;
      d2 = dx * dx + dy * dy;
      if(d2 > LINK2) continue;
      ctx.strokeStyle = 'rgba(51,112,255,' + ((1 - Math.sqrt(d2) / LINK) * 0.42).toFixed(3) + ')';
      ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
    }
  }
  ctx.stroke();
  ctx.lineWidth = 0.9;
  ctx.strokeStyle = 'rgba(51,112,255,.62)';
  ctx.beginPath();
  for(i = 0; i < pts.length; i++){
    a = pts[i];
    dx = a.x - NET.mx; dy = a.y - NET.my;
    if(dx * dx + dy * dy > MOUSE2) continue;
    for(j = 0; j < pts.length; j++){
      if(j === i) continue;
      b = pts[j];
      dx = a.x - b.x; dy = a.y - b.y;
      d2 = dx * dx + dy * dy;
      if(d2 > LINK2) continue;
      ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
    }
  }
  ctx.stroke();
  /* 点：鼠标附近的点更实、更大一点 */
  for(i = 0; i < pts.length; i++){
    a = pts[i];
    dx = a.x - NET.mx; dy = a.y - NET.my;
    var near = (dx * dx + dy * dy) < MOUSE2;
    ctx.fillStyle = 'rgba(51,112,255,' + (near ? 0.8 : 0.5) + ')';
    ctx.beginPath(); ctx.arc(a.x, a.y, a.r + (near ? 0.6 : 0), 0, Math.PI * 2); ctx.fill();
  }
  if(!NET_REDUCED && NET.on) NET.raf = requestAnimationFrame(netFrame);
}
function startLoginNet(){
  if(!NET.cvs){
    NET.cvs = document.getElementById('loginNet');
    if(!NET.cvs || !NET.cvs.getContext) return;      /* 老浏览器：没有 canvas 就不画，登录不受影响 */
  }
  var wrap = document.getElementById('loginWrap');
  if(!wrap || wrap.hidden) return;
  if(!NET.bound){
    NET.bound = true;
    /* 鼠标只在这层遮罩上跟着走；登录成功后退场动画期间也仍然有效，不影响。 */
    wrap.addEventListener('mousemove', function(e){
      var r = wrap.getBoundingClientRect();
      NET.mx = e.clientX - r.left; NET.my = e.clientY - r.top;
    });
    wrap.addEventListener('mouseleave', function(){ NET.mx = -9999; NET.my = -9999; });
    window.addEventListener('resize', function(){
      if(!NET.on) return;
      netSize(); netSeed();
      if(NET_REDUCED) netFrame();
    });
    /* 窗口切走 / 最小化：停。回来再继续 —— 后台 tab 里跑 rAF 本来就是浪费，
       但最小化的窗口在某些环境下 rAF 仍会被调度。 */
    document.addEventListener('visibilitychange', function(){
      if(document.hidden){ stopLoginNet(); }
      else if(LOGIN_SHOWN && !LOGIN_LEAVING){ startLoginNet(); }
    });
  }
  netSize();
  netSeed();
  if(NET.raf){ cancelAnimationFrame(NET.raf); NET.raf = 0; }
  if(NET_REDUCED){
    /* 降级：只画一帧静态网，不跑循环。NET.on 保持 false —— 它是"循环在跑"的
       标志，静态帧不需要它，否则窗口 resize 时会意外启动循环。 */
    NET.on = false;
    netFrame();
  }else{
    NET.on = true;
    netFrame();
  }
}
function stopLoginNet(){
  NET.on = false;
  if(NET.raf){ cancelAnimationFrame(NET.raf); NET.raf = 0; }
  /* 清干净：登录页隐藏后画布内容还留着，下次打开会先闪一帧旧图 */
  if(NET.ctx){ try{ NET.ctx.clearRect(0, 0, NET.w, NET.h); }catch(e){} }
}

/* ---------- 取数 ---------- */
async function load(){
  var j = await api('/api/candidates' + query());
  ITEMS = j.items || [];
  PAGE = j.paging || {page:1, page_size:0, total:ITEMS.length, total_pages:1};
  FACETS = j;                       /* 筛选器的人数（级联） */
  renderList();
  renderPager();
  renderFacetCounts();
  fillUnitSel(ITEMS);
}
/* 面试部门下拉的选项：当前这批人的 `interview_unit` 并集 + 字典里的单位。
   不只靠字典，是因为实际数据里常有字典外的历史写法；每次加载顺手刷新，
   选项才跟着数据走（与 facets 的做法同理）。 */
function fillUnitSel(items){
  var sel = document.getElementById('fUnit');
  if(!sel) return;
  var cur = sel.value, set = {};
  var fac = (FACETS && FACETS.interview_unit_facets) || {};
  Object.keys(fac).forEach(function(k){
    if(k) set[k] = Number(fac[k]) || 0;
  });
  (items || []).forEach(function(x){
    String(x.interview_unit || '').split(/[、,，;；]+/).forEach(function(u){
      u = u.trim();
      if(u && set[u] == null) set[u] = null;
    });
  });
  ((IDICT && IDICT.units) || []).forEach(function(u){
    if(u && set[u] == null) set[u] = null;
  });
  var keys = Object.keys(set).sort();
  sel.innerHTML = '<option value="">面试部门：不限</option>' + keys.map(function(k){
    return '<option value="' + esc(k) + '"' + (k === cur ? ' selected' : '') + '>' +
      esc(k) + (set[k] == null ? '' : '（' + set[k] + '）') + '</option>';
  }).join('');
  sel.value = cur;
}
async function pick(id){
  CUR = id;
  MODE = 'detail';
  COMPARE_IDS = []; COMPARE_DATA = {}; COMPARE_ACTIVE = null;
  highlightRow(id);          /* 只换高亮，不重建列表（见 highlightRow 的说明） */
  var d = await api('/api/candidates/' + id);
  if(d.__err){ document.getElementById('detail').innerHTML = '<div class="hint">' + esc(d.__err) + '</div>'; return; }
  CUR_DETAIL = d;
  MODE = 'detail';
  /* 面试字典要先拿到 —— 面试单位/时段/方式的下拉候选都来自它 */
  if(!IDICT_LOADED){
    IDICT_LOADED = true;
    api('/api/interview-dict').then(function(j){
      IDICT = j || {};
      if(MODE === 'detail' && CUR === d.id) openDetail(d);
    });
  }
  openDetail(d);   /* 审计内容由 openDetail 自己在渲染后填充（见其末尾） */
}

/* 搜索框：**输入不搜，清空才搜**（业务方 2026-10-21）。
   两条要求合起来的正确行为是：
     · 一边打字一边不查 —— 否则列表不停闪、输入被打断（原来的 260ms 防抖就是这个毛病）；
     · 但**清空 = 取消搜索**，应当立刻回到全部候选人 —— 让用户再点一次「搜索」才恢复，
       他会以为"清空没生效"。
   所以这里只在 value 变空时触发；有内容一律等「搜索」按钮或回车。 */
var kwTimer = null;
document.getElementById('kw').addEventListener('input', function(e){
  var v = ((e.target && e.target.value) || '').trim();
  if(v) return;                                   // 有内容 → 等用户点搜索/回车
  clearTimeout(kwTimer);
  kwTimer = setTimeout(doSearch, 120);            // 清空 → 恢复全量
});
['fEdu','fStage','fGender','fUniv','fBachelor','fCet','fDate','fOutcome','fOverseas','fUnit'].forEach(function(id){
  var el = document.getElementById(id);
  if(el) el.addEventListener('change', load);
});
/* 点击/回车触发搜索：**回到第 1 页**再查。
   不重置的话，从第 3 页搜一个新词会"没有结果"—— 其实是有结果但都在前两页。
   原版 `doSearch()` 也是这个口径（`ui.py:1787`）。 */
function doSearch(){
  PAGE.page = 1;
  load();
}

/* ============================================================
   写邮件（按原版口径重做：**两步式**）
   ------------------------------------------------------------
   第一步：选收件人 + 选模板 + 上传附件 + 填**运行时变量** → 生成草稿
   第二步：看附件/改主题/改正文 → 确认发送

   为什么必须有"运行时变量"表单（上一版最大的缺口）：
   模板里的 {面试时间}/{面试单位}/{联系人} 取不到值就变成【待填：xxx】，
   光靠正文手打补不齐 —— 而服务端**会拦住带【待填】的邮件**，等于发不出去。
   变量控件的"输入方式"也照原版：日期用日期选择器、时段/地点/单位/方式用
   **组合框**（点开能选、也能手输）、联系人选完**自动带出电话**、线上面试才显示会议号。
   ============================================================ */
var VIEW = 'pool';
var PIPE_PAGE = {};
var IDICT = {}, IDICT_LOADED = false;
var MAIL_TPL = [], MAIL_VARS = {builtin: [], runtime: []}, IDICT = {};
var MAIL_CANDS = [], MAIL_FULL = [];
var M_CID = null, M_AID = null, M_TO = '', M_SMTP = {}, M_CAND = null;
var M_OUTBOX = [];          /* 临时上传的附件 {outbox_id, filename, size} */
var M_RESULT = null;        /* 发送结果（成功凭据或失败原因） */
var M_SENDING = false;
var M_MAIL_META = {to: '', subject: ''};
var M_MAIL_REQ_ID = (function(){
  try{ return sessionStorage.getItem('tp_mail_req_id') || ''; }catch(e){ return ''; }
})();
var M_MAIL_ACTIVE_TASK = '';
var M_MAIL_TASK_ACK = '';

/* 当前模块记在 **URL hash** 里：整页刷新（F5）后还停在原模块，
   不会莫名其妙跳回人才库。浏览器前进/后退也跟着走。 */
var VIEWS = ['pool', 'chat', 'mail', 'org', 'archive', 'sys', 'users'];
function setView(v, fromHash){
  VIEW = v;
  if(!fromHash){
    var want = '#' + v;
    if(location.hash !== want){
      try{ history.replaceState(null, '', want); }catch(e){ location.hash = want; }
    }
  }
  document.body.className = 'v-' + v;
  /* ⚠️ 不能用 id.slice(0,-4) 反推视图名 —— 'archView' 去掉 4 个字符是 'arch'，
     而视图名是 'archive'，对不上 → 归档页永远不显示。
     用显式映射，别耍小聪明。 */
  var MAP = {mailView:'mail', orgView:'org', archView:'archive',
             sysView:'sys', chatView:'chat', usersView:'users'};
  Object.keys(MAP).forEach(function(id){
    var el = document.getElementById(id);
    if(el) el.classList.toggle('on', MAP[id] === v);
  });
  Array.prototype.forEach.call(document.querySelectorAll('.side button'), function(b){
    b.classList.toggle('on', b.dataset.view === v);
  });
  if(v === 'mail') initMail();

  if(v === 'org') initOrg();
  if(v === 'archive') initArch();
  if(v === 'sys') initSys();
  if(v === 'users') initUsers();
  if(v === 'chat') initChat();
}
/* 进页面时按 hash 恢复模块；hash 变了（前进/后退）也跟着切 */
(function restoreView(){
  var h = (location.hash || '').replace('#', '');
  if(VIEWS.indexOf(h) >= 0 && h !== 'pool') setView(h, true);
  window.addEventListener('hashchange', function(){
    var h2 = (location.hash || '').replace('#', '');
    if(VIEWS.indexOf(h2) >= 0 && h2 !== VIEW) setView(h2, true);
  });
})();
Array.prototype.forEach.call(document.querySelectorAll('.side button'), function(b){
  if(b.disabled) return;
  b.addEventListener('click', function(){ setView(b.dataset.view); });
});

/* ---------- 模板 → 需要哪些输入框 ----------
   模板正文里写着的 {变量} 才是这个模板真正需要的输入项。
   选「面试邀请」→ 出面试时间/时段/地点/单位/方式/会议号/联系人/电话；
   选「录用接收函」（正文里一个变量都没有）→ 一个输入框都不出。
   **不选模板**（手写）→ 全给，因为手写时也可能想用 {变量}。 */
function tplNeeded(tpl){
  if(!tpl) return null;                       // null = 不限
  var txt = (tpl.body || '') + '\n' + (tpl.subject || '');
  var need = {};
  var re = /\{([^{}]+)\}/g, m;
  while((m = re.exec(txt))){ need[m[1].trim()] = 1; }
  return need;
}
function curTpl(){
  var id = (document.getElementById('mTpl') || {}).value;
  if(!id) return null;
  return MAIL_TPL.filter(function(t){ return String(t.id) === String(id); })[0] || null;
}
function mailVarsHtml(){
  var need = tplNeeded(curTpl());
  var rows = (MAIL_VARS.runtime || []).filter(function(v){
    return !need || need[v.key];
  }).map(function(v){
    return '<div class="mrow"><label>' + esc(v.key) + '</label>' +
      '<div style="flex:1;min-width:0">' + varField(v.key, v.desc) + '</div></div>';
  }).join('');
  if(!rows){
    return '<div class="hint" style="margin:2px 0 8px">' +
      (curTpl() ? '这个模板正文里没有变量，不需要填任何东西。'
                 : '选一个模板，就只显示那个模板用得上的输入项。') + '</div>';
  }
  return rows;
}
/* 只重画变量区（换模板时用），画完要把已带出的值重新带回来 */
function renderMailVars(){
  var box = document.getElementById('mVars');
  if(!box) return;
  box.innerHTML = mailVarsHtml();
  bindMailVars();
  autofillInterviewVars(M_CAND);
}
function bindMailVars(){
  Array.prototype.forEach.call(document.querySelectorAll('#mVars [data-var]'), function(el){
    var k = el.dataset.var;
    el.addEventListener('input', function(){ mailVarChanged(k); });
    el.addEventListener('change', function(){ mailVarChanged(k); });
  });
}
function setMailRequestId(v){
  M_MAIL_REQ_ID = String(v || '');
  try{
    if(M_MAIL_REQ_ID) sessionStorage.setItem('tp_mail_req_id', M_MAIL_REQ_ID);
    else sessionStorage.removeItem('tp_mail_req_id');
  }catch(e){}
}
function newMailRequestId(){
  try{
    if(window.crypto && crypto.randomUUID) return crypto.randomUUID();
  }catch(e){}
  return 'mail-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
}
function setMailSending(on){
  M_SENDING = !!on;
  var b = document.getElementById('btnMailSend');
  if(b){
    b.disabled = M_SENDING;
    b.textContent = M_SENDING ? '发送中…' : '确认发送';
  }
}
function finishMailSent(r){
  var missAtt = (r && (r.attachments_missing || r.attachments_skipped)) || [];
  M_RESULT = {ok: true, to: M_MAIL_META.to, subject: M_MAIL_META.subject,
              at: new Date().toLocaleString(), res: r || {},
              missing: missAtt, res_written: ((r && r.written) || {})};
  M_OUTBOX = [];
  setMailRequestId('');
  M_MAIL_ACTIVE_TASK = '';
  setMailSending(false);
  renderOutbox();
  renderSent();
  if(missAtt.length){
    toast('邮件已发出，但 ' + missAtt.length + ' 个附件没带上：' + missAtt.join('、')
          + '（请重新上传后单独补发）', 'warn');
  }else{
    toast('已发送至 ' + M_MAIL_META.to, 'ok');
  }
}
function handleMailTask(task){
  if(!task) return;
  var tid = String(task.task_id || '');
  if(tid && M_MAIL_TASK_ACK === tid && !M_SENDING) return;
  M_MAIL_TASK_ACK = tid;
  closeProgDlg();
  M_MAIL_ACTIVE_TASK = '';
  var sent = task.result || (task.extra || {}).result || {};
  if(task.status !== 'done' || !sent.ok){
    var err = sent.error || task.message || '发送失败';
    M_RESULT = {ok: false, msg: err};
    setMailRequestId('');
    setMailSending(false);
    renderSent();
    toast('发送失败：' + err, 'danger');
    return;
  }
  finishMailSent(sent);
}
function attachMailTask(tid, meta){
  if(!tid || M_MAIL_ACTIVE_TASK === String(tid)) return;
  M_MAIL_ACTIVE_TASK = String(tid);
  var m = (meta && (meta.meta || meta)) || {};
  if(m.to) M_MAIL_META.to = m.to;
  if(m.subject) M_MAIL_META.subject = m.subject;
  setMailSending(true);
  M_RESULT = null;
  progressModal(tid, {
    title: '发送邮件',
    desc: '邮件正在后台发送，请勿重复点击。附件较大时会多等一会儿。',
    chain: true,
    onDone: handleMailTask
  });
}
async function resumeMailTask(){
  if(M_MAIL_ACTIVE_TASK) return true;
  var r = await api('/api/tasks/active?kind=mail&include_done=1');
  var tasks = (r && r.tasks) || [];
  var task = tasks.filter(function(t){
    return M_MAIL_REQ_ID && ((t.meta || {}).client_request_id === M_MAIL_REQ_ID);
  })[0];
  if(!task && !M_MAIL_REQ_ID){
    task = tasks.filter(function(t){ return t.status === 'running'; })[0];
  }
  if(!task) return false;
  if(task.meta){
    if(task.meta.to) M_MAIL_META.to = task.meta.to;
    if(task.meta.subject) M_MAIL_META.subject = task.meta.subject;
  }
  if(task.status === 'running'){
    attachMailTask(task.task_id, task);
  }else{
    handleMailTask(task);
  }
  return true;
}
/* ⚠️ 这里原本是 `async function initMail(){`，一次批量替换把 async 吃掉了，
   变成"async 挂在插入块前面、真正的 initMail 变成普通函数" → 里面的 await 报语法错、整页死。 */
async function initMail(){
  var r = await Promise.all([
    api('/api/mail/templates'), api('/api/interview-dict'),
    api('/api/mail/smtp'), api('/api/candidates')
  ]);
  MAIL_TPL = (r[0].items || []);
  MAIL_VARS = {builtin: r[0].builtin_vars || [], runtime: r[0].runtime_vars || []};
  IDICT = r[1] || {};
  M_SMTP = r[2] || {};
  MAIL_FULL = (r[3].items || []);
  MAIL_CANDS = MAIL_FULL.map(function(c){
    return {id: c.id, name: c.name || '', email: c.email || ''};
  });
  renderMail();
  setMailSending(M_SENDING);
  resumeMailTask();
}

/* ---------- 模板变量控件：按 key 分派成真控件（不是一排灰提示文本框） ---------- */
/* datalist id：变量名是中文，直接 encodeURIComponent 去掉 % 即可，
   不用正则 —— 这段 JS 在 Python 三引号里，regex 里的反斜杠会被当成转义序列。 */
function dlId(k){ return 'dl_' + encodeURIComponent(String(k)).replace(/%/g, ''); }
function combo(key, list, ph){
  /* ⚠️ 不用内联 oninput="f('x')"：这段 JS 在 Python 三引号里，\' 的反斜杠会被吃掉，
     渲染出来变成 f('' + k + '') → 整段脚本语法错、整页白。改用 data-var + 事件绑定。 */
  return '<input id="mv_' + esc(key) + '" list="' + dlId(key) + '" ' +
    'data-var="' + esc(key) + '" ' +
    'placeholder="' + esc(ph || '可直接选择，也能自己输入') + '">' +
    '<datalist id="' + dlId(key) + '">' + (list || []).map(function(x){
      return '<option value="' + esc(x) + '"></option>';
    }).join('') + '</datalist>';
}
function varField(key, desc){
  if(key === '面试时间'){
    return '<input id="mv_面试时间" type="date" data-var="面试时间">';
  }
  if(key === '面试时段') return combo(key, IDICT.slots, '如 09:00-10:00');
  if(key === '面试地点') return combo(key, IDICT.rooms, '选会议室，或直接输入');
  if(key === '面试单位') return combo(key, IDICT.units, '选单位，或直接输入');
  if(key === '面试方式') return combo(key, IDICT.modes, '现场面试 / 线上面试');
  if(key === '会议号'){
    return '<input id="mv_会议号" placeholder="线上面试的会议号或入会链接">' +
      '<div class="miss" id="hintMeet" style="display:none">选了线上面试就要填会议号，' +
      '否则候选人收不到入会方式</div>';
  }
  if(key === '联系人'){
    var opts = (IDICT.contacts || []).map(function(c){
      return '<option value="' + esc(c.name) + '"></option>';
    }).join('');
    return '<input id="mv_联系人" list="dl_联系人" data-var="联系人" ' +
      'placeholder="点选或直接输入姓名">' +
      '<datalist id="dl_联系人">' + opts + '</datalist>' +
      '<div class="hint" style="margin-top:3px">选好后自动带出电话；字典在「系统配置 → 面试字典」维护</div>';
  }
  if(key === '联系电话'){
    return '<input id="mv_联系电话" readonly placeholder="选联系人后自动带出">';
  }
  return '<input id="mv_' + esc(key) + '" placeholder="' + esc(desc || '') + '">';
}
function mailVarChanged(key){
  if(key === '面试方式'){
    var mode = (document.getElementById('mv_面试方式') || {}).value || '';
    var online = mode.indexOf('线上') >= 0;
    var box = document.getElementById('mv_会议号');
    var hint = document.getElementById('hintMeet');
    if(box) box.style.display = online ? '' : 'none';
    if(hint) hint.style.display = online ? '' : 'none';
    if(!online && box) box.value = '';
    if(online && box && !box.value && IDICT.meeting_no) box.value = IDICT.meeting_no;
  }
  if(key === '联系人'){
    var who = ((document.getElementById('mv_联系人') || {}).value || '').trim();
    var tel = document.getElementById('mv_联系电话');
    if(!tel) return;
    var hit = (IDICT.contacts || []).filter(function(c){ return c.name === who; })[0];
    tel.value = hit ? (hit.phone || '') : '';
    tel.placeholder = hit ? '' : '字典里没这个人，请手动填写';
  }
}

/* ---------- 收件人：点开列全部、输入前缀筛选、↑↓ 选、Enter 确认 ---------- */
var AC_IDX = -1;
function filterCand(clearSel){
  var box = document.getElementById('mCandDrop');
  if(!box) return;
  var kw = ((document.getElementById('mCandInput') || {}).value || '').trim().toLowerCase();
  if(clearSel){
    /* 输入内容一旦变化，就必须解除“已选候选人”绑定。
       否则先选 A、再输入 B，发送时仍会用 A 的邮箱和 application_id。 */
    M_CID = null; M_AID = null; M_CAND = null; M_TO = '';
    clearAutoVars();
    var toBox = document.getElementById('mTo');
    if(toBox) toBox.innerHTML = '<span class="hint">请从下拉结果中点选候选人</span>';
  }
  var list = MAIL_CANDS.filter(function(x){
    return !kw || (x.name || '').toLowerCase().indexOf(kw) >= 0
      || (x.email || '').toLowerCase().indexOf(kw) >= 0;
  }).slice(0, 80);
  AC_IDX = list.length ? 0 : -1;
  box.innerHTML = list.map(function(x, i){
    return '<div class="acItem' + (i === AC_IDX ? ' sel' : '') + '" data-id="' + x.id + '">' +
      '<b>' + esc(x.name || '未识别') + '</b>' +
      (x.email ? ' <span class="hint">' + esc(x.email) + '</span>' : ' <span class="hint">无邮箱</span>') +
      '</div>';
  }).join('') || '<div class="acItem">没有匹配的候选人</div>';
  box.classList.add('on');
  Array.prototype.forEach.call(box.querySelectorAll('.acItem[data-id]'), function(it){
    it.addEventListener('mousedown', function(ev){
      ev.preventDefault(); pickCand(Number(it.dataset.id));
    });
  });
}
function candKey(ev){
  var box = document.getElementById('mCandDrop');
  var items = box.querySelectorAll('.acItem[data-id]');
  if(!items.length) return;
  if(ev.key === 'ArrowDown' || ev.key === 'ArrowUp'){
    ev.preventDefault();
    AC_IDX = (AC_IDX + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    Array.prototype.forEach.call(items, function(x, i){ x.classList.toggle('sel', i === AC_IDX); });
  }else if(ev.key === 'Enter'){
    ev.preventDefault();
    if(AC_IDX >= 0) pickCand(Number(items[AC_IDX].dataset.id));
  }
}
async function pickCand(cid){
  document.getElementById('mCandDrop').classList.remove('on');
  var d = await api('/api/candidates/' + cid);
  if(d.__err){ toast(d.__err); return; }
  /* **先撤掉上一个人的自动带出值**，再带新人的 ——
     否则换人后旧人的面试单位/时间会留在框里（业务方 14:5x 明确要求"重选要覆盖"）。 */
  clearAutoVars();
  M_CID = cid; M_AID = d.application_id || null;
  M_CAND = d;
  M_TO = (d.email && d.email !== '—') ? d.email : '';
  document.getElementById('mCandInput').value = d.name || '';
  renderToChip(d);
  /* 变量区可能还没画出来（第一次进来是先画模板再选人）→ 没有就等选模板时再带 */
  autofillInterviewVars(d);
  /* 正文里有 {姓名} 等变量 → 换人要重新生成，否则信里还是上一个人的名字 */
  if(document.getElementById('mTpl').value) genDraft();
  renderMailAtt();
}
/* 清空收件人：连同**自动带出的**面试信息一起清掉（HR 手填的保留）。 */
function clearRecipient(){
  clearAutoVars();
  M_CID = null; M_AID = null; M_CAND = null; M_TO = '';
  document.getElementById('mCandInput').value = '';
  document.getElementById('mTo').innerHTML =
    '<span class="hint">已清空收件人</span>';
  toast('已清空收件人及其带出的信息');
}
/* 收件人邮箱必须**一眼看清**（邮箱错一位就是发错人），不做小灰字 */
function renderToChip(d){
  var box = document.getElementById('mTo');
  if(!M_TO){
    box.innerHTML = '<span class="miss">这个人没有邮箱 —— 请在下面手动填写收件人地址</span>' +
      '<input id="mToManual" placeholder="收件人邮箱" style="margin-top:6px">';
    return;
  }
  box.innerHTML = '<span class="tochip">' + esc(M_TO) +
    '<b onclick="clearRecipient()" title="清空收件人（连同自动带出的信息一起清）">×</b></span>' +
    '<span class="hint" style="margin-left:8px">' + esc(d.name || '') + '</span>';
}
/* ---------- 档案里填过的面试信息 → 自动带进邮件页 ----------
   业务方 2026-10-20 14:4x：「我在完整档案填写了面试信息，选面试邀请模板应该自动带出来」。
   原版只带出"面试单位"一个字段，这里扩到档案里实际有的那几个：
   面试单位 / 面试时间 / 面试时段 / 面试方式。
   原则不变：**只在值为空时填**（HR 手填过的绝不覆盖）。
   地点/联系人/电话档案里没有 → 留给 HR 从面试字典里选。 */
/* ---------- 自动带出的值要能"撤回" ----------
   业务方 2026-10-20 14:5x：「删除人选应该删除对应信息，重选人选应该覆盖信息」。
   所以不能只是"值为空才填"——那样换人时旧人的值会留着（发错人）。
   做法：**记住哪些值是自动带出的**（M_AUTO）。
     · 值被 HR 改过 → 变成"手填"，不再被自动流程覆盖；
     · 值还是自动带出来的 → 换人/删人时先清掉，再带新人的。 */
var M_AUTO = {};
function setAutoVar(key, val){
  var el = document.getElementById('mv_' + key);
  if(!el) return;                              // 该模板不需要这个变量 → 没有输入框
  var cur = (el.value || '').trim();
  if(M_AUTO[key] !== undefined && M_AUTO[key] !== cur){
    delete M_AUTO[key];                        // HR 改过了 → 算手填
    return;
  }
  if(!val) return;
  el.value = val;
  M_AUTO[key] = val;
}
/* 这几个字段是**由候选人档案自动带出来的**（不是 HR 自由填的），
   所以"删人/换人"时必须一起清掉或覆盖 ——
   留着上一个���的信息，写给下一个人就是错信息。
   纯手填的字段（面试地点 / 联系人 / 联系电话）不在这个名单里，**不动**。 */
var AUTO_KEYS = ['面试单位', '面试时间', '面试时段', '面试方式'];
function clearAutoVars(){
  AUTO_KEYS.forEach(function(k){
    var el = document.getElementById('mv_' + k);
    if(el) el.value = '';
  });
  M_AUTO = {};
}
function autofillInterviewVars(d){
  var src = d || M_CAND || (MAIL_FULL.filter(function(x){ return x.id === M_CID; })[0]) || {};
  if(!src || !src.id) return;
  var map = {
    '面试单位': src.interview_unit,
    '面试时间': src.interview_date ? String(src.interview_date).slice(0, 10) : '',
    '面试时段': src.interview_slot,
    '面试方式': src.interview_mode
  };
  Object.keys(map).forEach(function(k){ setAutoVar(k, map[k] || ''); });
}

/* ---------- 渲染整页 ---------- */
function renderMail(){
  var tlist = MAIL_TPL.map(function(t){
    return '<option value="' + t.id + '">' + esc(t.name) +
      (t.scene ? '（' + esc(t.scene) + '）' : '') + '</option>';
  }).join('');
  var rvars = mailVarsHtml();
  var chips = [].concat(MAIL_VARS.builtin || [], MAIL_VARS.runtime || []).map(function(v){
    return '<code title="' + esc(v.desc || '') + '">{' + esc(v.key) + '}</code>';
  }).join(' ');

  document.getElementById('mailView').innerHTML =
    '<div class="mform">' +
      '<div class="dhead" style="margin-bottom:6px"><div style="min-width:0">' +
        '<div class="dname">写邮件</div>' +
        '<div class="dmeta"><span>发送必须由你点确认；变量取不到值会标成【待填：xxx】，带【待填】的邮件发不出去</span></div>' +
      '</div></div>' +
      '<div class="sec" style="margin-top:10px"><h3>发信账号</h3>' +
        (M_SMTP.user
          ? '<div>' + esc(M_SMTP.user) + '　<span class="hint">' + esc(M_SMTP.host || '') +
            ':' + esc(String(M_SMTP.port || '')) + '　' + (M_SMTP.ssl ? 'SSL' : 'STARTTLS') + '</span>' +
            '　<button class="btn" onclick="checkSmtp()">检查发信配置</button>' +
            ' <span class="hint" id="smtpMsg"></span></div>'
          : '<div class="miss">未配置发信账号 —— 去「系统配置 → 邮箱接入」配好才能发</div>') +
      '</div>' +

      '<div class="sec"><h3>第一步 · 选人、选模板</h3>' +
        '<div class="mrow"><label>收件人</label>' +
          '<div class="ac" style="position:relative">' +
            '<input id="mCandInput" placeholder="输入姓名，点选或回车确认" autocomplete="off" ' +
              'oninput="filterCand(true)" onfocus="filterCand()" onkeydown="candKey(event)">' +
            '<div class="acList" id="mCandDrop"></div></div></div>' +
        '<div id="mTo" style="margin:6px 0 10px"></div>' +
        '<div class="mrow"><label>模板</label>' +
          '<select id="mTpl"><option value="">（不用模板，直接手写）</option>' + tlist + '</select></div>' +
        '<div class="mrow" style="align-items:flex-start"><label>附件</label>' +
          '<div style="flex:1;min-width:0">' +
            '<button class="btn" onclick="pickAttach()">上传附件</button>' +
            ' <span class="hint" id="upAttMsg">图片 / PDF / Word / Excel / PPT / OFD 等常见格式，单份 ≤20MB、合计 ≤20MB</span>' +
            '<input id="upAttach" type="file" hidden multiple '
              + 'accept=".pdf,.doc,.docx,.docm,.dot,.dotx,.xls,.xlsx,.xlsm,.xlsb,.xltx,'
              + '.ppt,.pptx,.pptm,.pps,.ppsx,.txt,.csv,.md,.rtf,'
              + '.odt,.ods,.odp,.wps,.et,.dps,.zip,.rar,.7z,'
              + '.ofd,.jpg,.jpeg,.jfif,.png,.webp,.bmp,.gif,.tif,.tiff,.heic,.heif,.avif" ' +
              'onchange="uploadOutbox(this.files)">' +
            '<div id="mOutBox" style="margin-top:6px"></div>' +
            '<div id="mAttSum" class="hint" style="margin-top:2px"></div>' +
          '</div></div>' +
        '<div id="mVars">' + rvars + '</div>' +
        '<div style="margin-top:12px"><button class="btn primary" onclick="genDraft()">生成草稿</button>' +
          '<span class="hint" style="margin-left:8px">这一步只生成内容，不会发送</span></div>' +
      '</div>' +

      '<div class="sec"><h3>可用变量</h3><div class="vhint">' + chips + '</div></div>' +
    '</div>' +
    '<div class="medit">' +
      '<div class="sec"><h3>第二步 · 预览与修改</h3>' +
        '<div class="mrow"><label>主题</label><input id="mSubject"></div>' +
        '<div id="mMiss" style="margin:6px 0"></div>' +
        '<div class="mrow" style="align-items:flex-start"><label style="padding-top:6px">正文</label>' +
          '<div style="flex:1;min-width:0">' +
            EDITOR_TOOLBAR('mBody') +
            '<div id="mBody" class="richeditor" contenteditable="true" ' +
              'oninput="onEditorInput(\'mBody\')" onpaste="return onEditorPaste(event)"></div>' +
          '</div></div>' +
        '<div class="medit-actions">' +
          '<button class="btn primary" id="btnMailSend" onclick="sendMail()"' +
            (M_SENDING ? ' disabled' : '') + '>' +
            (M_SENDING ? '发送中…' : '确认发送') + '</button> ' +
          '<button class="btn" onclick="saveAsTemplate()">把当前正文存为模板</button>' +
          '<span class="hint">后台发送，不会卡住当前页面</span>' +
        '</div>' +
        '<div id="mailSentBox" style="margin-top:10px"></div>' +
      '</div>' +
    '</div>';

  document.getElementById('mTpl').addEventListener('change', function(){
    /* 换模板 → 输入框要跟着换（只显示这个模板用得上的）→ 再把档案里的面试信息带回来
       → 最后生成草稿 */
    renderMailVars();
    genDraft();
  });
  /* 变量控件的联动统一在这里绑定（不写内联事件，见上面 combo 的注释） */
  Array.prototype.forEach.call(document.querySelectorAll('.mform [data-var]'), function(el){
    var k = el.dataset.var;
    el.addEventListener('input', function(){ mailVarChanged(k); });
    el.addEventListener('change', function(){ mailVarChanged(k); });
  });
  Array.prototype.forEach.call(document.querySelectorAll('.mtool [data-cmd]'), function(el){
    el.addEventListener('click', function(){ fmt(el.dataset.cmd); });
  });
  document.getElementById('mCandInput').addEventListener('blur', function(){
    setTimeout(function(){
      var b = document.getElementById('mCandDrop');
      if(b) b.classList.remove('on');
    }, 180);
  });
  renderMailAtt();
  renderOutbox();
  if(M_RESULT) renderSent();
}

/* ---------- 页内确认弹窗（替代原生 confirm / prompt） ---------- */
function closeDlg(v){
  var r = window.__dlgResolve;
  window.__dlgResolve = null;
  if(r) r(v);
  closeProgDlg();
}
function askConfirm(opts){
  var o = (typeof opts === 'string') ? {body: opts} : (opts || {});
  return new Promise(function(resolve){
    document.getElementById('dlgTitle').textContent = o.title || '请确认';
    var editorBody = document.getElementById('edDlgBody');
    if(editorBody){ editorBody.style.display = 'none'; editorBody.innerHTML = ''; }
    document.getElementById('dlgBody').style.display = '';
    document.getElementById('dlgBody').textContent = o.body || '';
    document.getElementById('dlgNo').textContent = o.cancelText || '取消';
    var yes = document.getElementById('dlgYes');
    yes.textContent = o.okText || '确定';
    yes.className = o.danger ? 'btn btn-danger' : 'btn primary';
    yes.onclick = function(){ closeDlg(true); };
    document.getElementById('dlgNo').onclick = function(){ closeDlg(false); };
    document.getElementById('mask').onclick = function(ev){
      if(ev.target === ev.currentTarget) closeDlg(false);
    };
    window.__dlgResolve = resolve;
    document.getElementById('mask').classList.add('on');
    yes.focus();
  });
}
function askText(title, def, ph){
  return new Promise(function(resolve){
    document.getElementById('dlgTitle').textContent = title;
    var editorBody = document.getElementById('edDlgBody');
    if(editorBody){ editorBody.style.display = 'none'; editorBody.innerHTML = ''; }
    document.getElementById('dlgBody').style.display = '';
    document.getElementById('dlgBody').innerHTML =
      '<input id="dlgInput" style="width:100%;height:34px;padding:0 10px;' +
      'border:1px solid var(--line-2);border-radius:var(--r-s);font:inherit" ' +
      'placeholder="' + esc(ph || '') + '" value="' + esc(def || '') + '">' +
      '<span class="hint" style="display:block;margin-top:8px">回车确定，Esc 取消</span>';
    document.getElementById('dlgNo').textContent = '取消';
    var yes = document.getElementById('dlgYes');
    yes.textContent = '确定'; yes.className = 'btn primary';
    function done(v){ closeDlg(v || null); }
    yes.onclick = function(){ done((document.getElementById('dlgInput').value || '').trim() || true); };
    document.getElementById('dlgNo').onclick = function(){ done(null); };
    var inp = document.getElementById('dlgInput');
    inp.addEventListener('keydown', function(ev){
      if(ev.key === 'Enter') done(inp.value.trim() || true);
      if(ev.key === 'Escape') done(null);
    });
    document.getElementById('mask').onclick = function(ev){
      if(ev.target === ev.currentTarget) done(null);
    };
    window.__dlgResolve = resolve;
    document.getElementById('mask').classList.add('on');
    setTimeout(function(){ inp.focus(); inp.select(); }, 30);
  });
}
/* 从一组选项里挑一个（单选）。
   为什么不用 askText：让 HR **凭记忆敲出岗位全名**，敲错一个字就"没有叫这个名字的岗位"，
   而正确做法是把已有的选择摆出来。`options` = [{value, label}]。 */
function askSelect(title, options, ph){
  return new Promise(function(resolve){
    document.getElementById('dlgTitle').textContent = title;
    var editorBody = document.getElementById('edDlgBody');
    if(editorBody){ editorBody.style.display = 'none'; editorBody.innerHTML = ''; }
    document.getElementById('dlgBody').style.display = '';
    document.getElementById('dlgBody').innerHTML =
      '<select id="dlgSelect" style="width:100%;height:36px;padding:0 9px;font-size:14.5px;' +
      'border:1px solid var(--line-2);border-radius:var(--r-s);background:var(--surface)">' +
      (ph ? '<option value="">' + esc(ph) + '</option>' : '') +
      (options || []).map(function(o){
        return '<option value="' + esc(o.value) + '">' + esc(o.label) + '</option>';
      }).join('') + '</select>';
    document.getElementById('dlgNo').textContent = '取消';
    var yes = document.getElementById('dlgYes');
    yes.textContent = '确定'; yes.className = 'btn primary';
    function done(v){ closeDlg(v || null); }
    yes.onclick = function(){ done(document.getElementById('dlgSelect').value || null); };
    document.getElementById('dlgNo').onclick = function(){ done(null); };
    document.getElementById('mask').onclick = function(ev){
      if(ev.target === ev.currentTarget) done(null);
    };
    window.__dlgResolve = resolve;
    document.getElementById('mask').classList.add('on');
    var sel = document.getElementById('dlgSelect');
    setTimeout(function(){ sel.focus(); }, 30);
  });
}
document.addEventListener('keydown', function(ev){
  if(ev.key === 'Escape' && document.getElementById('mask').classList.contains('on')) closeDlg(false);
});

/* ============================================================
   分析看板：原版的"投递进度环"搬回来 + 几张**不重复**的维度图
   + 一个「条件勾选 → 自动出图」的自助分析区
   ------------------------------------------------------------
   为什么环和条形图不重复：
     · 环   = **人在流程的哪一步**（阶段分布）—— 原版 v1.25.6 的设计
     · 条形 = **质量分级 / 学历层次 / 院校出身 / 面试部门分工** —— 四个不同维度
   每张图回答一个不同的问题，堆在一起不重复。
   ============================================================ */
var ANA = {items: [], stats: null, facets: null, conds: {}, dim: 'stage'};

/* ---------- 自助分析的条件：**种类是固定的**（下面这份清单），
   **显示哪些由使用者自己勾**（存本地，不写进服务端设置）。
   为什么分开：条件种类会随业务增加（今天有面试单位、明天可能有渠道），
   但每个 HR 关心的维度不一样 —— 他不该被别人的选择绑住。 */
var ANA_CONDS = [
  {k:'edu',     label:'学历',     fixed:['博士','硕士','研究生','本科','大专','未标注']},
  {k:'gender',  label:'性别',     fixed:['男','女','未标注']},
  {k:'stage',   label:'阶段',     dyn:'stage'},
  {k:'unit',    label:'面试单位', dyn:'unit'},
  {k:'job',     label:'岗位',     dyn:'job'},
  {k:'school',  label:'院校层次', fixed:['985','211','强势','未识别']},
  {k:'cet',     label:'四六级',   fixed:['六级','四级','未提供']},
  {k:'channel', label:'投递来源', dyn:'channel'},
  {k:'skill',   label:'技能',     dyn:'skill'},
  {k:'hasMail', label:'有邮箱',   fixed:['1']},
  {k:'hasDoc',  label:'有简历附件', fixed:['1']},
  {k:'review',  label:'需复核',   fixed:['1']}
];
var ANA_CONDS_SEL = null;
function anaSelLoad(){
  if(ANA_CONDS_SEL) return ANA_CONDS_SEL;
  try{
    var raw = localStorage.getItem('ana_conds');
    ANA_CONDS_SEL = raw ? JSON.parse(raw) : ['edu','gender','stage','unit'];
  }catch(e){ ANA_CONDS_SEL = ['edu','gender','stage','unit']; }
  if(!ANA_CONDS_SEL.length) ANA_CONDS_SEL = ['edu'];
  return ANA_CONDS_SEL;
}
function anaSelSave(list){
  ANA_CONDS_SEL = list;
  try{ localStorage.setItem('ana_conds', JSON.stringify(list)); }catch(e){}
}
/* 某个条件维度的可选值（从数据里现取，固定清单优先） */
function anaCondOpts(c){
  if(c.fixed) return c.fixed;
  var items = ANA.items, m = {};
  items.forEach(function(x){
    var v = (c.k === 'stage') ? x.stage
            : (c.k === 'unit') ? String(x.interview_unit || '').split(/[、，,]/)[0]
            : (c.k === 'job') ? x.job_title
            : (c.k === 'channel') ? x.channel
            : (c.k === 'skill') ? (x.skills || [])[0]
            : null;
    (Array.isArray(v) ? v : [v]).forEach(function(x2){
      if(x2) m[x2] = 1;
    });
  });
  return Object.keys(m);
}
/* 配置入口：勾选要显示哪些条件 */
function anaConfigConds(){
  var sel = anaSelLoad();
  var html = '<div class="anagrp" style="margin-bottom:4px">' +
    ANA_CONDS.map(function(c){
      return '<label class="anack"><input type="checkbox" data-ck="' + c.k + '"' +
        (sel.indexOf(c.k) >= 0 ? ' checked' : '') + '> ' + esc(c.label) + '</label>';
    }).join('') + '</div>' +
    '<div class="hint">勾上的条件会出现在自助分析区；取消勾选只是不显示，不影响其它功能。</div>';
  openProgDlg('配置分析条件', html +
    '<div class="bar" style="margin-top:16px;justify-content:flex-end">' +
      '<button class="btn primary" onclick="anaSaveConds()">保存</button></div>');
  Array.prototype.forEach.call(document.querySelectorAll('#edDlgBody [data-ck]'), function(cb){
    cb.addEventListener('change', function(){
      var list = [];
      Array.prototype.forEach.call(document.querySelectorAll('#edDlgBody [data-ck]'), function(x){
        if(x.checked) list.push(x.dataset.ck);
      });
      anaSelSave(list);
    });
  });
}
function anaSaveConds(){ closeProgDlg(); anaRenderCustom(); }

/* ---- 原版的投递进度环（原样搬：点环段/图例 → 跳人才库并按该阶段筛选）---- */
function stageRing(st, opts){
  var o = opts || {};
  var COLORS = STAGE_CFG.colors || {};
  var order = STAGE_CFG.order || [];
  var segs = order.map(function(k){ return [k, (st[k] || 0), COLORS[k] || '#a7b0bf']; });
  var extra = 0;
  Object.keys(st || {}).forEach(function(k){ if(order.indexOf(k) < 0) extra += st[k] || 0; });
  if(extra && segs.length) segs[0][1] += extra;     /* 兜底：库里出现新阶段时不丢人 */
  var total = segs.reduce(function(a, x){ return a + x[1]; }, 0);
  if(!total) return '<div class="hint">投递进度：暂无（还没有投递）</div>';
  var R = 30, CIRC = 2 * Math.PI * R, acc = 0;
  var arcs = segs.filter(function(x){ return x[1] > 0; }).map(function(x){
    var k = x[0], v = x[1], color = x[2];
    var len = CIRC * v / total;
    var el = '<circle class="td-seg" r="' + R + '" cx="38" cy="38" fill="none" ' +
      'stroke="' + color + '" stroke-width="15" ' +
      'stroke-dasharray="' + len + ' ' + (CIRC - len) + '" stroke-dashoffset="' + (-acc) + '" ' +
      'transform="rotate(-90 38 38)" onclick="anaPickStage(\'' + k + '\')">' +
      '<title>' + k + '：' + v + ' 人 —— 点击筛选</title></circle>';
    acc += len;
    return el;
  }).join('');
  var legend = segs.map(function(x){
    return '<div class="lgitem" onclick="anaPickStage(\'' + x[0] + '\')" ' +
      'title="' + x[0] + '：' + x[1] + ' 人 —— 点击筛选">' +
      '<i style="background:' + x[2] + '"></i><span>' + x[0] + '</span><b class="num">' + x[1] + '</b></div>';
  }).join('');
  return '<div class="tier-donut">' +
    '<svg width="' + (o.size || 142) + '" height="' + (o.size || 142) + '" viewBox="0 0 76 76" role="img" ' +
      'aria-label="投递进度">' + arcs +
      '<text class="td-total" x="38" y="38" text-anchor="middle" dominant-baseline="central" ' +
        'font-size="16" font-weight="600" fill="var(--ink)" onclick="anaClear()">' +
        '<title>全部 ' + total + ' 人 —— 点击清除所有筛选</title>' + total + '</text></svg>' +
    '<div class="donutlegend">' + legend + '</div></div>';
}
/* 环形图中心的总数：点它 = 清除所有筛选。
   ⚠️ 这个函数原来**从来没定义过**（`onclick="anaClear()"` 与 title 都写着"点击清除所有筛选"），
   点了直接 ReferenceError —— 界面上承诺了一个不存在的动作（业务方 2026-10-21 复查发现）。
   复用 `clearFilters()`（它已经负责清下拉 + TAB 复位 + 重新加载），
   再把自助分析自己勾的条件一并清掉，否则"清了筛选但分析图还是窄的"。 */
function anaClear(){
  ANA.conds = {};
  clearFilters();
  anaRenderCustom();
  toast('已清除所有筛选', 'ok');
}
function anaPickStage(k){
  var sel = document.getElementById('fStage');
  if(sel){ sel.value = k; }
  setView('pool');
  /* ⚠️ 一定要**重新加载列表**：只设了筛选值、不重查，左侧看着就是"没反应"。
     切页本身不触发 load（只有人才库以外的分页才各自初始化）。 */
  load();
}
/* ---- 横向条形图（纯 CSS，不引图表库）---- */
function barChart(rows, opts){
  var o = opts || {};
  if(!rows.length) return '<div class="hint">没有数据</div>';
  var max = Math.max.apply(null, rows.map(function(r){ return r[1]; })) || 1;
  return '<div class="barchart">' + rows.map(function(r){
    var pct = Math.round(r[1] * 100 / max);
    return '<div class="brow"><span class="blabel" title="' + esc(r[0]) + '">' + esc(r[0]) + '</span>' +
      '<span class="btrack"><i style="width:' + pct + '%;background:' + (r[2] || 'var(--accent)') + '"></i></span>' +
      '<span class="bnum num">' + r[1] + '</span></div>';
  }).join('') + '</div>';
}
/* ---- 统计助手：把候选人列表聚成 [标签, 人数, 颜色] ---- */
function tally(items, pick){
  var m = {};
  items.forEach(function(x){
    var vs = pick(x) || [];
    (Array.isArray(vs) ? vs : [vs]).forEach(function(v){
      if(v === null || v === undefined || v === '') return;
      m[v] = (m[v] || 0) + 1;
    });
  });
  return Object.keys(m).map(function(k){ return [k, m[k]]; })
    .sort(function(a, b){ return b[1] - a[1]; });
}
/* ---- 自助分析：条件勾选 → 自动出图 ---- */
var ANA_DIMS = {
  stage:     {label: '按阶段',   get: function(x){ return [x.stage]; }},
  edu:       {label: '按学历',   get: function(x){ return [x.edu_level]; }},
  school:    {label: '按院校层次', get: function(x){
                var t = x.tier_school || x.uni_tier || '';
                return [t || '未识别']; }},
  unit:      {label: '按面试单位', get: function(x){ return [x.interview_unit]; }},
  job:       {label: '按岗位',   get: function(x){ return [x.job_title]; }},
  skill:     {label: '按技能',   get: function(x){ return x.skills || []; }},
  channel:   {label: '按投递来源', get: function(x){ return [x.channel]; }}
};
function anaItems(){
  var c = ANA.conds;
  return ANA.items.filter(function(x){
    if(c.edu && c.edu.length && c.edu.indexOf(x.edu_level) < 0) return false;
    if(c.gender && c.gender.length && c.gender.indexOf(x.gender) < 0) return false;
    if(c.stage && c.stage.length && c.stage.indexOf(x.stage) < 0) return false;
    if(c.unit && c.unit.length){
      var u = String(x.interview_unit || '').split(/[、，,]/).map(function(t){ return t.trim(); });
      if(!u.some(function(t){ return c.unit.indexOf(t) >= 0; })) return false;
    }
    if(c.school && c.school.length){
      var sc = x.uni_tier || x.tier_school || '未识别';
      if(c.school.indexOf(sc) < 0) return false;
    }
    if(c.cet && c.cet.length){
      var ct = x.cet_level || '未提供';
      if(c.cet.indexOf(ct) < 0) return false;
    }
    if(c.channel && c.channel.length && c.channel.indexOf(x.channel) < 0) return false;
    if(c.skill && c.skill.length){
      var sk = x.skills || [];
      if(!sk.some(function(v){ return c.skill.indexOf(v) >= 0; })) return false;
    }
    if(c.hasMail && !x.email) return false;
    if(c.hasDoc && !x.resume_doc_id) return false;
    if(c.review && c.review.length && !x.needs_review) return false;
    if(c.job && c.job.length && c.job.indexOf(x.job_title) < 0) return false;
    return true;
  });
}
function anaRenderCustom(){
  var box = document.getElementById('anaCustom');
  if(!box) return;
  var items = anaItems();
  /* 把统计基数**如实写出来**（业务方 2026-10-21）：以前这里写死"最多 200 条"，
     现在是全量，所以要让人看见"这是按多少人算出来的"——
     勾了条件时更要说明基数变成了当次筛选后的人数，否则容易把两次数的口径搞混。 */
  var scope = document.getElementById('anaScope');
  if(scope){
    var nAll = ANA.items.length, nSel = items.length;
    scope.textContent = (ANA.items.length && items.length !== nAll)
      ? ('统计范围：勾选条件后 ' + nSel + ' 人 / 全部 ' + nAll + ' 人')
      : ('统计范围：全部 ' + nAll + ' 人');
  }
  var SEL = anaSelLoad();
  var dim = ANA_DIMS[ANA.dim] || ANA_DIMS.stage;
  var rows = tally(items, dim.get);
  if(ANA.dim === 'skill') rows = rows.slice(0, 12);
  else rows = rows.slice(0, 12);
  var COL = STAGE_CFG.colors || {};
  rows = rows.map(function(r){
    var c = ANA.dim === 'stage' ? (COL[r[0]] || 'var(--accent)') : 'var(--accent)';
    return [r[0], r[1], c];
  });
  var condTxt = Object.keys(ANA.conds).filter(function(k){ return (ANA.conds[k] || []).length; })
    .map(function(k){
      var names = {edu: '学历', gender: '性别', stage: '阶段', unit: '面试单位', job: '岗位'};
      return names[k] + '=' + ANA.conds[k].join('/');
    }).join('　');
  /* 条件区：只渲染用户勾选的维度 */
    /* 条件区：只渲染用户勾选的维度。
       选项排成**网格**而不是 flex-wrap —— 原来用 flex 换行，每行能放几个全看字数，
       "面试单位"只有一个选项时独占一整行，看起来就是散的（业务方 2026-10-21 截图）。 */
  var condHtml = SEL.map(function(k){
    var c = ANA_CONDS.filter(function(item){ return item.k === k; })[0];
    if(!c) return '';
    var opts = anaCondOpts(c);
    if(!opts.length) return '';
    return '<div class="anagrp"><span class="anagname">' + esc(c.label) + '</span>' +
      '<div class="anackgrid">' + opts.map(function(v){
        var on = (ANA.conds[c.k] || []).indexOf(v) >= 0;
        return '<label class="anack' + (on ? ' on' : '') + '">' +
          '<input type="checkbox" ' + (on ? 'checked' : '') +
          ' onchange="anaCond(\'' + c.k + '\',' + jq(v) + ',this.checked)"> ' + esc(v) + '</label>';
      }).join('') + '</div></div>';
  }).filter(Boolean).join('');
  box.innerHTML =
    '<div class="anacondhead">' +
      '<span class="anacount"><b class="num">' + items.length + '</b> 人符合条件' +
        (condTxt ? '（' + esc(condTxt) + '）' : '（未加条件，全量）') + '</span>' +
      '<button class="btn sm" onclick="anaConfigConds()">配置条件项</button>' +
    '</div>' +
    '<div class="ana-workbench"><div class="ana-controls">' +
      (condHtml || '<div class="hint">还没有启用筛选条件，点「配置条件」添加。</div>') +
    '</div><div class="ana-result">' +
    '<div class="anadim">分析维度：' + Object.keys(ANA_DIMS).map(function(k){
      return '<button class="dimbtn' + (ANA.dim === k ? ' on' : '') + '" onclick="anaSetDim(\'' + k + '\')">' +
        ANA_DIMS[k].label + '</button>';
    }).join('') + '</div>' +
    barChart(rows) +
    '</div></div></div>';
}
function anaSetDim(k){ ANA.dim = k; anaRenderCustom(); }
function anaCond(key, val, on){
  if(!ANA.conds[key]) ANA.conds[key] = [];
  var i = ANA.conds[key].indexOf(val);
  if(on && i < 0) ANA.conds[key].push(val);
  if(!on && i >= 0) ANA.conds[key].splice(i, 1);
  anaRenderCustom();
}
/* 只负责**拉数据**（概览面板里用），不再自己占一个视图。
   `stats` 可由调用方传入 —— 概览面板自己已经拉过一次 `/api/stats`，
   再拉一遍等于同一次打开页面发两次同样的请求（实测概览页共 4 个请求）。 */
async function anaLoad(stats){
  if(ANA.items.length){ setTimeout(anaRenderCustom, 0); return; }
  /* ⚠️ 这里**不要**加 `page_size`（2026-10-21 修复）。
     `/api/candidates` 的分页参数是"人才库界面的阅读方式"，**不传就是全量** ——
     这是后端刻意保留的兼容口径（`server.py:778-781`：「两者都不传（或传 0）时
     返回全量……程序化消费方依赖"一次拿全"，不应被迫翻页」），而且注释里给了实测：
     1000 人全量装配 59ms。
     原来这里写死 `?page_size=200`，于是自助分析的每张图都只统计了前 200 个人 ——
     而HR 要的恰恰是"全院这批人整体什么样子"，抽样 200 会让结论直接失真。 */
  var r = await Promise.all([
    stats ? Promise.resolve(stats) : api('/api/stats'),
    api('/api/candidates')
  ]);
  ANA.stats = r[0] || {};
  ANA.items = (r[1].items || []);
  ANA.facets = r[1] || {};
  anaRenderCustom();
  killAutofill();
}

/* ============================================================
   岗位管理（左列表 + 右详情；JD 的输入方式照搬原版）
   ============================================================ */
var JOBS = [], JOB_CUR = null;
async function initOrg(){
  var box = document.getElementById('orgView');
  box.innerHTML = '<div class="hint" style="padding:20px">读取岗位…</div>';
  var j = await api('/api/jobs');
  JOBS = j.items || [];
  /* ⚠️ 类名**不能**用 `top2`（2026-10-20 修复）：
     `.top2` 是人才库那条工具栏的类，而 CSS 里有一组"非人才库视图让位"规则
     `body.v-org .top2{display:none!important}`（见本文件 CSS 区）——
     岗位管理页的 body 正好是 `v-org`，于是「新增岗位」按钮连同整条栏被一起隐藏，
     用户既看不到也点不到，岗位功能从源头不可用。
     这里改用独立类名 `jobbar`，与人才库 chrome 完全解耦。 */
  box.innerHTML =
    '<div class="orgleft">' +
      '<div class="list" id="jobList"></div>' +
      /* 「新增岗位」放在左列**下方**（业务方 2026-10-21：列表左上、新增下方、
         详情在右）。原来它横在顶部、又因为多出一个 grid 子元素把详情挤成窄列。 */
      '<div class="jobbar" id="jobBar">' +
        '<button class="btn btn-primary btn-lg" data-job="new">新增岗位</button>' +
        '<span class="hint">共 ' + JOBS.length + ' 个岗位</span></div>' +
    '</div>' +
    '<div class="detail" id="jobDetail"><div class="hint">左边选一个岗位</div></div>';
  document.getElementById('jobList').innerHTML = JOBS.length
    ? JOBS.map(function(x){
        return '<div class="row jrow" data-id="' + x.id + '">' +
          '<div class="rowbar"></div><div class="av" style="border-radius:6px">' +
            esc((x.title || '?').slice(0, 1)) + '</div>' +
          '<div style="min-width:0"><div class="nm">' + esc(x.title || '未命名') + '</div>' +
            '<div class="sub">' + esc(x.department_name || x.dept || '未指定部门') +
            '　· 在池 ' + (x.applications_count || 0) + ' 人</div></div>' +
          '<div class="jrowacts">' +
            '<span class="jst ' + (x.active ? 'on' : 'off') + '">' +
            esc(x.active ? '在招' : '停用') + '</span>' +
            '<button class="btn sm" onclick="event.stopPropagation();openJob(' + x.id + ')">编辑</button>' +
          '</div></div>';
      }).join('')
    : '<div class="empty">还没有岗位</div>';
  Array.prototype.forEach.call(
    document.querySelectorAll('#jobList .row'), function(r){
      r.addEventListener('click', function(){ openJob(Number(r.dataset.id)); });
    });
  var bar = document.getElementById('jobBar');
  if(bar && !bar.dataset.bound){
    bar.dataset.bound = '1';
    bar.addEventListener('click', function(ev){
      var t = ev.target.closest ? ev.target.closest('[data-job]') : null;
      if(t) newJob();
    });
  }
  if(JOBS.length) openJob(JOBS[0].id);
}
/* 新增岗位（原版岗位管理有"新增岗位"入口，v2 之前只能改不能加） */
/* 新增岗位（原版也是一张表单：岗位名/部门/招聘数量/JD 尺子）。
   服务端 JobReq 接受 title/department_id/headcount/education_min/years_min/
   must_skills/major_required/note —— 一次建好，不用建完再逐项补。 */
async function newJob(){
  openProgDlg('新增岗位',
    '<div class="crow"><label>岗位名称</label><div class="cinput">' +
      '<input class="ced" id="njTitle"></div></div>' +
    '<div class="crow"><label>招聘数量</label><div class="cinput">' +
      '<input class="ced" id="njHead" type="number" min="0" step="1" style="width:90px"> 人</div></div>' +
    '<div class="crow"><label>最低学历</label><div class="cinput">' +
      '<select class="ced" id="njEdu" style="width:120px">' +
        ['', '大专', '本科', '硕士', '博士'].map(function(v){
          return '<option value="' + v + '">' + (v || '不限') + '</option>';
        }).join('') + '</select></div></div>' +
    '<div class="crow"><label>最低年限</label><div class="cinput">' +
      '<input class="ced" id="njYears" type="number" min="0" step="1" style="width:90px"> 年</div></div>' +
    '<div class="crow"><label>必需技能</label><div class="cinput">' +
      '<input class="ced" id="njSkills" placeholder="顿号分隔"></div></div>' +
    '<div class="crow"><label>专业要求</label><div class="cinput">' +
      '<input class="ced" id="njMajors" placeholder="顿号分隔"></div></div>' +
    '<div class="bar" style="margin-top:16px;justify-content:flex-end">' +
      '<button class="btn primary" onclick="saveNewJob()">创建</button></div>');
}
async function saveNewJob(){
  var g = function(x){ var e = document.getElementById(x); return e ? e.value.trim() : ''; };
  var title = g('njTitle');
  if(!title){ toast('岗位名称不能为空'); return; }
  var split = function(s){
    return (s || '').split(/[、,，]/).map(function(x){ return x.trim(); }).filter(Boolean);
  };
  var body = {title: title};
  if(g('njHead') !== '') body.headcount = Number(g('njHead'));
  if(g('njEdu')) body.education_min = g('njEdu');
  if(g('njYears') !== '') body.years_min = Number(g('njYears'));
  /* ⚠️ 空数组**不能发**：后端 `_jd_from_req` 用 `is not None` 判定，
     收到 `[]` 会把它当成"显式清空"，覆盖掉 config/jd.json 的默认尺子。
     原意是"没填就继承默认尺子"，发 `[]` 反而让新岗位的判定口径悄悄变宽
     （什么技能都算命中）。所以只在真有内容时才带这两个字段。 */
  var mustSkills = split(g('njSkills'));
  var majors = split(g('njMajors'));
  if(mustSkills.length) body.must_skills = mustSkills;
  if(majors.length) body.major_required = majors;
  var r = await api('/api/jobs', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if(r.__err){ toast(r.__err); return; }
  closeProgDlg();
  toast('已新增岗位：' + title);
  initOrg();
  if(r.id) openJob(r.id);
}
async function openJob(jid){
  JOB_CUR = jid;
  loadOntologyHints();          /* 专业/技能规范名候选（异步，不阻塞渲染） */
  setTimeout(function(){
    attachHint('jdMajors', ONTOLOGY_MAJORS);
    attachHint('jdSkills', ONTOLOGY_SKILLS);
    attachHint('jdPref', ONTOLOGY_SKILLS);
  }, 700);
  Array.prototype.forEach.call(document.querySelectorAll('#jobList .row'), function(r){
    r.classList.toggle('on', Number(r.dataset.id) === jid);
  });
  var d = await api('/api/jobs/' + jid);
  if(d.__err){ return; }
  /* ⚠️ `/api/jobs/{id}` 返回的是 **{job, jd}** 两层（列表接口才是平铺的）——
     直接读 d.title 会拿到 undefined，页面显示「未命名」。 */
  var J = d.job || d;
  var jd = d.jd || J.jd_json || {};
  var must = jd.must || {};
  var box = document.getElementById('jobDetail');
  var eduOpts = ['', '大专', '本科', '硕士', '博士'].map(function(v){
    return '<option value="' + v + '"' + ((must.education_min || '') === v ? ' selected' : '') +
      '>' + (v || '不限') + '</option>';
  }).join('');
  box.innerHTML =
    '<div class="dhead"><div style="min-width:0">' +
      '<div class="dname">' + esc(J.title || '未命名') + '</div>' +
      '<div class="dmeta"><span>' + esc(J.department_name || J.dept || '未指定部门') + '</span>' +
        '<span class="chipl gray">' + esc(J.owner || '未指定负责人') + '</span>' +
        '<span class="chipl gray">编制 ' + (J.headcount == null ? '—' : J.headcount) + '</span>' +
        '<span class="chipl gray">在池 ' + (J.applications_count || 0) + ' 人</span></div>' +
    '</div><div class="dact">' +
      '<button class="btn primary" data-act="save">保存 JD</button>' +
      '<button class="btn" data-act="regrade">按此 JD 重判档位</button>' +
      (J.active
        ? '<button class="btn" data-act="deactivate">停用</button>'
        : '<button class="btn" data-act="activate">启用</button>') +
    '</div></div>' +
    '<div class="sec"><h3>岗位要求（决定档位尺子）</h3>' +
      '<div class="bar2"><span class="lbl2">最低学历</span>' +
        '<select id="jdEdu" style="width:130px">' + eduOpts + '</select>' +
        '<span class="lbl2">最低年限</span>' +
        '<input id="jdYears" type="number" min="0" step="1" style="width:90px" value="' +
          esc(String(must.years_min == null ? 0 : must.years_min)) + '">' +
      '</div>' +
      '<div class="bar2"><span class="lbl2">必需技能</span>' +
        '<input id="jdSkills" style="flex:1;min-width:0" value="' +
          esc((must.skills_required || []).join('、')) + '" ' +
          'placeholder="顿号分隔，如 金相分析、X射线衍射"></div>' +
      '<div class="bar2"><span class="lbl2">专业要求</span>' +
        '<input id="jdMajors" style="flex:1;min-width:0" value="' +
          esc((must.major_required || []).join('、')) + '" placeholder="顿号分隔"></div>' +
      /* 「加分技能」原本读的是 `must.skills_preferred`，而后端 `_jd_from_req`
         把它组装到 `preferred.skills`（见 server.py:4095，config/jd.json 也是这个结构）
         —— 位置对不上，这一栏永远显示空白。已改为真实位置。
         「必备项 must_have」已移除：整个后端（JobJdReq / regrade）都没有这个字段，
         填什么都不影响判定，是个会误导人的假控件（原版也没有）。 */
      '<div class="bar2"><span class="lbl2">加分技能</span>' +
        '<input id="jdPref" style="flex:1;min-width:0" value="' +
          esc(((J.jd_json && J.jd_json.preferred && J.jd_json.preferred.skills) || []).join('、')) +
          '" placeholder="加分项，有更好，没有不算减分"></div>' +
      '<div class="bar2"><span class="lbl2">招聘数量</span>' +
        '<input id="jdHead" type="number" min="0" step="1" style="width:90px" value="' +
          esc(String(J.headcount == null ? '' : J.headcount)) + '"> 人</div>' +
      '<div class="bar2"><span class="lbl2">岗位说明</span>' +
        '<textarea id="jdNote" rows="3" style="flex:1;min-width:0" ' +
          'placeholder="岗位职责、必须说明的事项（自由文本）">' +
          esc(J.jd_json && J.jd_json.note ? J.jd_json.note : '') + '</textarea></div>' +
    '</div>' +
    (J.opened_at ? '<div class="sec"><h3>时间</h3><dl class="kv">' +
      '<dt>开放</dt><dd>' + esc(String(J.opened_at).slice(0, 16)) + '</dd>' +
      '<dt>关闭</dt><dd>' + esc(String(J.closed_at || '').slice(0, 16) || '—') + '</dd>' +
      '</dl></div>' : '');
  Array.prototype.forEach.call(box.querySelectorAll('.dact [data-act]'), function(b){
    b.addEventListener('click', function(){ orgAct(b.dataset.act); });
  });
}
/* ---------- 重判档位：两段式（先预演，看完差异再落库） ----------
   2026-10-20 修复。原来这里是 `POST /api/jobs/{id}/regrade` 且**不带 body**，
   而后端 `RegradeReq.apply` 默认是 False（`server.py:4305-4307`，刻意设计成
   "先给差异、HR 看过再点应用"，防一次误点把全岗位建议档位全改掉）——
   结果**一条都没改**，界面还提示「已重判 N 条投递」；而且读的是 `r.regraded`，
   后端返回的字段叫 `changed`，恒为 0，属于**报假成功**。
   现在照原版（`ui.py:4681-4737`）做成两段式：预演出逐人对比表，人确认后再应用。 */
async function regradeJob(jid, apply){
  setModalTitle('重新分析 · 按当前 JD 重算已有投递');
  document.getElementById('edDlgBody').innerHTML =
    '<div class="hint">正在按当前 JD 重算该岗位下的投递…'
    + '（从简历原文重跑规则通道，不调模型，结果可重复）</div>';
  openEditorDlg();
  var r = await api('/api/jobs/' + jid + '/regrade',
    {method:'POST', headers:{'Content-Type':'application/json'},
     body: JSON.stringify({apply: !!apply})});
  if(r.__err){
    document.getElementById('edDlgBody').innerHTML =
      '<div style="color:var(--bad);white-space:pre-wrap">' + esc(r.__err) + '</div>' +
      '<div class="bar" style="margin-top:14px;justify-content:flex-end">' +
        '<button class="btn primary" onclick="closeProgDlg()">关闭</button></div>';
    return;
  }
  var tspan = function(k){
    return '<span class="tier ' + esc(k || '') + '">' + esc(k || '—') + '</span>';
  };
  var rows = (r.items || []).map(function(x){
    var diff;
    if(x.skipped){
      diff = '<span class="hint">' + esc(x.skipped) + '</span>';
    }else if(x.kept && x.old_tier !== x.new_tier){
      /* 已人工确认过的档位即便有新建议也不动 —— 如实呈现，并标出"未修改" */
      diff = '<span class="hint">建议 ' + esc(x.old_tier || '—') + ' → ' + esc(x.new_tier || '—')
        + '（<b>HR 已确认，未改动</b>）</span>';
    }else if(x.changed){
      diff = '<b>' + tspan(x.old_tier) + '</b> → <b>' + tspan(x.new_tier) + '</b>'
        + '<div class="hint">建议档位' + (r.applied ? '已更新' : '将更新') + '</div>';
    }else{
      diff = '<span class="hint">无变化</span>';
    }
    var why = (x.changed && (x.reasons || []).length)
      ? '<div class="hint">' + esc((x.reasons || []).slice(0, 2).join('；')) + '</div>' : '';
    var note = x.note ? '<div class="hint">' + esc(x.note) + '</div>' : '';
    return '<tr><td><b>' + esc(x.name || '未识别') + '</b>'
      + '<div class="hint">投递 #' + x.application_id + '</div></td>'
      + '<td class="hint">' + esc(x.tier_source || '') + '</td>'
      + '<td>' + diff + '</td><td>' + why + note + '</td></tr>';
  }).join('');
  document.getElementById('edDlgBody').innerHTML =
    '<div class="hint">岗位 <b>' + esc((r.job || {}).title || '') + '</b> ｜尺子来源 '
      + esc(r.jd_source || '') + ' ｜共 ' + (r.total || 0) + ' 条：建议档位'
      + (r.applied ? '变化' : '将变化') + ' <b>' + (r.changed || 0) + '</b> 条、'
      + '已确认未改动 ' + (r.kept_hr_confirmed || 0) + ' 条、无法重算 '
      + (r.cannot_regrade || 0) + ' 条</div>' +
    '<div style="margin-top:10px;padding:8px 10px;border-radius:6px;' +
      'background:var(--surface-2);font-size:14px">' + esc(r.note || '') + '</div>' +
    '<table style="margin-top:12px;width:100%;border-collapse:collapse"><thead><tr>' +
      '<th style="text-align:left">候选人</th><th style="text-align:left">档位来源</th>' +
      '<th style="text-align:left">建议档位</th><th style="text-align:left">原因 / 说明</th>' +
      '</tr></thead><tbody>' +
      (rows || '<tr><td colspan="4">该岗位下没有投递记录</td></tr>') +
      '</tbody></table>' +
    (r.channel_note ? '<div class="hint" style="margin-top:10px">' + esc(r.channel_note) + '</div>' : '') +
    '<div class="bar" style="margin-top:12px;display:flex;gap:8px;align-items:center">' +
      ((!r.applied && r.changed)
        ? '<button class="btn primary" onclick="regradeJob(' + jid + ', true)">应用重算结果（改 '
          + r.changed + ' 条）</button>' : '') +
      '<button class="btn" onclick="closeModal();initOrg();openJob(' + jid + ')">关闭</button>' +
      '<span class="hint">重算只改「系统建议」，不动阶段、不动备注、不动 HR 已确认的档位。</span>' +
    '</div>';
  /* 落库后立刻重画：否则弹层关掉之前看到的还是应用前的旧数字——
     「点了应用，可数字没变」是最容易被怀疑"根本没生效"的地方。 */
  if(r.applied) await initOrg();
}
async function orgAct(act){
  var jid = JOB_CUR;
  if(act === 'save'){
    /* JD 要发**扁平**字段（2026-10-20 修复）：
       原来这里把表单拼成嵌套的 {must:{...}} 直接发出去，可后端 `JobJdReq`
       （server.py:4058-4066）收的是扁平字段，`_jd_from_req` 才负责组装成
       库里的嵌套结构。发嵌套 → Pydantic 静默忽略 → 学历/年限/必需技能/加分技能/
       专业要求**全部不落库**，界面却照样提示「JD 已保存」。
       结果就是"界面显示的尺子"和"库里生效的尺子"不是一回事，判定口径直接失真。 */
    function splitList(id){
      var e = document.getElementById(id);
      return (e && e.value || '').split(/[、,，]/).map(function(s){ return s.trim(); })
        .filter(Boolean);
    }
    /* 只提交**真正填了**的项：后端 `_jd_from_req` 的语义是「未填沿用默认尺子」，
       提交空值会变成"把已有尺子清空"（原版 ui.py:4756-4760 也是这么处理）。 */
    var must = splitList('jdSkills');
    var pref = splitList('jdPref');
    var majors = splitList('jdMajors');
    var edu = (document.getElementById('jdEdu') || {}).value || '';
    var yrsRaw = (document.getElementById('jdYears') || {}).value;
    var noteEl = document.getElementById('jdNote');
    var body = {};
    if(must.length) body.must_skills = must;
    if(pref.length) body.preferred_skills = pref;
    if(majors.length) body.major_required = majors;
    if(edu) body.education_min = edu;
    /* 「不限年限」= 0（后端 0 即不设门槛），校招岗位常用这一项 */
    if(yrsRaw !== '' && yrsRaw != null) body.years_min = Number(yrsRaw);
    var hc = document.getElementById('jdHead');
    if(hc && hc.value !== '') body.headcount = Number(hc.value);
    if(noteEl && noteEl.value.trim()) body.note = noteEl.value.trim();
    if(!Object.keys(body).length){
      toast('没有改动任何一项；清空某一项请直接在原版尺子文件里改','warn');
      return;
    }
    var r = await api('/api/jobs/' + jid + '/jd', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if(r.__err){
      toast('保存失败：' + r.__err, 'danger');
      return;
    }
    var savedNote = 'JD 已保存';
    if((r.warnings || []).length) savedNote += '；' + r.warnings[0];
    else if(r.can_regrade) savedNote += '；可点「按此 JD 重判档位」';
    toast(savedNote, 'ok');
    /* 重画让新值立刻可见；但 initOrg() 末尾会打开第一个岗位，
       所以保存完要再切回刚编辑的那个——否则人会被"弹"走，不知道保存到哪儿去了。 */
    await initOrg();
    if(jid) await openJob(jid);
    return;
  }
  if(act === 'regrade'){ await regradeJob(jid, false); return; }
  var ep = '/api/jobs/' + jid + '/' + (act === 'activate' ? 'activate' : 'deactivate');
  var r3 = await api(ep, {method:'POST'});
  toast(r3.__err ? ('失败：' + r3.__err) : (act === 'activate' ? '已启用' : '已停用'));
  initOrg();
}

/* ============================================================
   归档（**逻辑照搬原版** viewArchive）
   ------------------------------------------------------------
   原版有四件事，v2 上一版只做了一件事：
     ① 批量取消归档（勾选的人）
     ② 批量彻底删除（勾选的人）—— 不可恢复
     ③ **按年归档**：把"最后一条投递早于 N 年"的整批归档（人太多不可能一个个勾）
     ④ 每条要显示**归档到期状态**（还有几天到期 / 已到期）
   布局仍用 v2 的主从两栏，但功能与原版一一对应。
   ============================================================ */
var ARCH_ITEMS = [], ARCH_SEL = {};

/* 归档到期徽章：days_left 为 null 表示"没有到期时间"，不显示。 */
function archLeftChip(x){
  var am = x.archive || {};
  var left = am.days_left;
  if(left == null) return '';
  if(left <= 0) return '<span class="achip exp">归档已到期</span>';
  var expiring = left <= 7;
  var bg = expiring ? 'var(--bad-soft)' : 'var(--surface-2)';
  var fg = expiring ? 'var(--bad)' : 'var(--ink-2)';
  return '<span class="achip" style="color:' + fg + ';background:' + bg + '" ' +
    'title="归档 ' + esc(am.purge_at || '') + ' 到期后自动彻底删除">' +
    '归档中（' + esc(String(am.purge_at || '').slice(0, 10)) + ' 到期）</span>';
}
async function initArch(){
  var box = document.getElementById('archView');
  if(!document.getElementById('archList')){
    var ys = [];
    var y0 = new Date().getFullYear();
    for(var v = 2018; v <= 2050; v++) ys.push(v);
    box.innerHTML =
      '<div class="archbar" id="archBar" style="display:flex;align-items:center;gap:8px;' +
        'padding:0 16px;flex-wrap:wrap">' +
        '<span id="archCount" class="hint"></span>' +
        '<span class="vdiv"></span>' +
        '<button class="btn" data-arch="un">批量取消归档</button>' +
        '<button class="btn danger" data-arch="purge">批量彻底删除</button>' +
        '<button class="btn" data-arch="download">批量下载选中</button>' +
        '<span class="vdiv"></span>' +
        '<span class="hint">按年归档</span>' +
        '<select id="archYear" style="height:32px;padding:0 8px;border:1px solid var(--line-2);' +
          'border-radius:var(--r-s);background:var(--surface)">' +
          ys.map(function(x){ return '<option value="' + x + '"' + (x === y0 ? ' selected' : '') +
            '>' + x + '</option>'; }).join('') +
        '</select><span class="hint">年以前</span>' +
        '<button class="btn primary" data-arch="year">归档 ' + y0 + ' 年以前的投递</button>' +
        '<span class="hint">按最后一条投递的年份整批归档，适合"新一年开始、旧简历收起来"</span>' +
      '</div>' +
      '<div class="list" id="archList"></div>' +
      '<div class="detail" id="archDetail"><div class="hint">左边选一个人</div></div>';
    document.getElementById('archYear').addEventListener('change', function(e){
      var y = e.target.value;
      e.target.dataset.year = y;
      var btn = document.querySelector('#archBar [data-arch="year"]');
      if(btn) btn.textContent = '归档 ' + y + ' 年以前的投递';
    });
  }
  var bar = document.getElementById('archBar');
  if(bar && !bar.dataset.bound){
    bar.dataset.bound = '1';
    bar.addEventListener('click', function(ev){
      var t = ev.target.closest ? ev.target.closest('[data-arch]') : null;
      if(t) archAct(t.dataset.arch);
    });
  }
  await loadArch();
}
async function loadArch(){
  var r = await api('/api/candidates?archived=1');
  var items = r.items || [];
  /* 双重保险：接口已按 archived=1 过滤，这里再挡一道 ——
     归档页**只**显示已归档的人。字段不存在时不筛，避免把整页清空。 */
  if(items.length && items[0].archived_at !== undefined){
    items = items.filter(function(x){ return !!x.archived_at; });
  }
  ARCH_ITEMS = items;
  document.getElementById('archList').innerHTML = items.length
    ? items.map(function(x){
        var on = ARCH_SEL[x.id];
        var av = x.avatar_path === '-' ? ''
          : '<img alt="" src="/api/candidates/' + x.id + '/avatar" onerror="this.remove()">';
        var left = archLeftChip(x);
        var contact = [x.phone, x.email].filter(function(v){ return v && v !== '—'; }).join(' · ');
        return '<div class="row' + (on ? ' on' : '') + '" data-id="' + x.id + '">' +
          '<div class="rowbar"></div>' +
          '<div class="chk"><input type="checkbox" data-id="' + x.id + '"' + (on ? ' checked' : '') + '></div>' +
          '<div class="av">' + esc((x.name || '?').slice(0, 1)) + av + '</div>' +
          '<div style="min-width:0">' +
            '<div class="nm">' + esc(x.name || '未识别') +
              (left ? ' ' + left : '') + '</div>' +
            '<div class="sub">' + esc([x.edu_level, x.school].filter(Boolean).join(' · ') || '—') + '</div>' +
            '<div class="sub">阶段 ' + esc(x.stage || '新投递') +
              '　归档于 ' + esc(String(x.archived_at || '').slice(0, 16) || '—') + '</div>' +
            (contact ? '<div class="sub"><b>联系方式</b>：' + esc(contact) + '</div>' : '') +
            (x.note ? '<div class="sub">' + esc(String(x.note).slice(0, 40)) + '</div>' : '') +
          '</div>' +
          '<div class="tier ' + esc(x.tier_effective || 'C') + '">' +
            esc(x.tier_effective || '–') + '</div></div>';
      }).join('')
    : '<div class="empty">还没有归档档案。在人才库上点「归档」即可移入这里。</div>';
  Array.prototype.forEach.call(document.querySelectorAll('#archList .row'), function(r0){
    r0.addEventListener('click', function(ev){
      if(ev.target.tagName !== 'INPUT') archPick(Number(r0.dataset.id));
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll('#archList .chk input'), function(c){
    c.addEventListener('change', function(){
      ARCH_SEL[c.dataset.id] = c.checked;
      var rr = document.querySelector('#archList .row[data-id="' + c.dataset.id + '"]');
      if(rr) rr.classList.toggle('on', c.checked);
      archCount();
    });
  });
  archCount();
  killAutofill();
}
function archCount(){
  var n = Object.keys(ARCH_SEL).filter(function(k){ return ARCH_SEL[k]; }).length;
  var el = document.getElementById('archCount');
  if(el) el.textContent = '共 ' + ARCH_ITEMS.length + ' 份已归档档案，已勾选 ' + n;
}
async function archPick(cid){
  var d = await api('/api/candidates/' + cid);
  if(d.__err) return;
  var box = document.getElementById('archDetail');
  box.innerHTML =
    '<div class="dhead"><div class="av-lg">' + esc((d.name || '?').slice(0, 1)) + '</div>' +
      '<div style="min-width:0"><div class="dname">' + esc(d.name || '未识别') + '</div>' +
      '<div class="dmeta"><span>' + esc(d.edu_level || '') + ' · ' + esc(d.school || '') + '</span>' +
      '<span class="chipl gray">阶段 ' + esc(d.stage || '新投递') + '</span>' +
      '<span class="chipl gray">归档于 ' + esc(String(d.archived_at || '').slice(0, 16) || '—') + '</span>' +
      '</div></div>' +
      '<div class="dact">' +
        '<button class="btn" data-act="detail">完整档案</button>' +
        '<button class="btn primary" data-act="un">取消归档</button>' +
        '<button class="btn danger" data-act="purge">彻底删除</button>' +
      '</div></div>' +
    '<div class="sec"><h3>联系方式</h3><div class="contactline">' +
      (d.phone && d.phone !== '—'
        ? '<span class="cval1">' + esc(d.phone) +
          '<button class="btn sm" onclick="copyText(\'' + esc(d.phone) + '\')">复制</button></span>' : '') +
      (d.email && d.email !== '—'
        ? '<span class="cval1">' + esc(d.email) +
          '<button class="btn sm" onclick="copyText(\'' + esc(d.email) + '\')">复制</button></span>' : '') +
      (!(d.phone && d.phone !== '—') && !(d.email && d.email !== '—') ? '未记录' : '') +
    '</div></div>' +
    '<div class="sec"><h3>简历原件</h3>' +
      ((d.resume_documents || []).length
        ? d.resume_documents.map(function(x){
            return '<div class="doc"><span class="doc-n">' + esc(x.file_name || '') + '</span>' +
              '<span class="doc-a"><button class="btn" onclick="openDoc(' + x.id + ',1)">打开</button>' +
              ' <button class="btn" onclick="openDoc(' + x.id + ',0)">下载</button></span></div>';
          }).join('')
        : '<div class="hint">无</div>') + '</div>';
  Array.prototype.forEach.call(box.querySelectorAll('.dact [data-act]'), function(b){
    b.addEventListener('click', function(){
      if(b.dataset.act === 'detail') archOpenFull(cid);
      else archAct(b.dataset.act, [cid]);
    });
  });
  killAutofill();
}
async function archOpenFull(cid){
  var d = await api('/api/candidates/' + cid);
  if(d.__err){ toast(d.__err, 'danger'); return; }
  CUR = cid;
  CUR_DETAIL = d;
  MODE = 'detail';
  setView('pool');
  openDetail(d);
}
/* 归档页的四个动作，**与原版一一对应** */
async function archAct(act, ids){
  if(act === 'un' || act === 'purge'){
    var list = ids || Object.keys(ARCH_SEL).filter(function(k){ return ARCH_SEL[k]; }).map(Number);
    if(!list.length){ toast('先勾选要处理的人'); return; }
    if(act === 'un'){
      var ok1 = await askConfirm({title: '批量取消归档',
        body: '将取消归档 ' + list.length + ' 人。\n\n取消归档后立即恢复在人才库与检索中展示。继续？',
        okText: '取消归档'});
      if(!ok1) return;
      var r1 = await api('/api/candidates/archive-batch', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ids: list, archived: false})});
      if(r1.__err){ toast(r1.__err); return; }
      ARCH_SEL = {};
      toast('已取消归档 ' + (r1.changed || 0) + ' 人' +
            (r1.skipped ? '（跳过 ' + r1.skipped + ' 人）' : ''));
      document.getElementById('archDetail').innerHTML = '<div class="hint">左边选一个人</div>';
      loadArch();
      return;
    }
    /* 彻底删除：不可恢复 —— 确认框里要把"会删什么"写清楚 */
    var names = ARCH_ITEMS.filter(function(x){ return list.indexOf(x.id) >= 0; })
      .map(function(x){ return x.name || '?'; });
    var shown = names.slice(0, 12).join('、') + (names.length > 12 ? ' 等' : '');
    var ok2 = await askConfirm({title: '批量彻底删除', danger: true, okText: '彻底删除',
      body: '即将彻底删除 ' + list.length + ' 份已归档档案：\n' + shown +
            '\n\n档案、投递、附件记录与原件文件都会删除，不可恢复。\n\n确定删除？'});
    if(!ok2) return;
    var okc = 0, fail = [];
    for(var i = 0; i < list.length; i++){
      var rr = await api('/api/candidates/' + list[i] + '/purge', {method:'POST'});
      if(rr.__err) fail.push(rr.__err); else okc++;
    }
    ARCH_SEL = {};
    toast('已彻底删除 ' + okc + ' 人' + (fail.length ? '，失败 ' + fail.length + ' 人' : ''));
    document.getElementById('archDetail').innerHTML = '<div class="hint">左边选一个人</div>';
    loadArch();
    return;
  }
  if(act === 'download'){
    var dl = ids || Object.keys(ARCH_SEL).filter(function(k){ return ARCH_SEL[k]; }).map(Number);
    if(!dl.length){ toast('先勾选要下载的人'); return; }
    /* 后端 `BundleReq` 只认 `names` / `document_ids`（`server.py:3096-3105`），
       原来这里发的是 `candidate_ids` → 后端收到两个空列表 → 400
       「没有选择任何文件」，按钮一点就报错。
       归档页勾选的是**候选人 id**，要换成效档人简历原件的附件 id：
       逐个取详情里的 resume_documents。勾选通常只有几位，不值得为它改后端。 */
    toast('正在取 ' + dl.length + ' 人的简历原件…');
    var docIds = [];
    for(var i = 0; i < dl.length; i++){
      var one = await api('/api/candidates/' + dl[i]);
      if(one.__err) continue;
      (one.resume_documents || []).forEach(function(x){ if(x && x.id) docIds.push(Number(x.id)); });
    }
    if(!docIds.length){
      toast('勾选的人里没有可下载的简历原件（原件可能不在归档区）', 'warn');
      return;
    }
    var rr = await fetch(withToken('/api/sources/bundle'),
      {method:'POST', headers: hdr(), body: JSON.stringify({document_ids: docIds})});
    if(!rr.ok){
      var msg = '打包失败（HTTP ' + rr.status + '）';
      try{ var jj = await rr.json(); msg = jj.detail || msg; }catch(e){}
      toast(msg, 'warn'); return;
    }
    /* 跳过明细走响应头（HTTP 头不能放非 ASCII，后端是百分号编码过的）：
       否则勾了十份、其中一份原件丢了，整批都下不来还不说明原因。 */
    var skipped = parseInt(rr.headers.get('X-Skipped') || '0');
    var got = parseInt(rr.headers.get('X-Batch-Count') || String(docIds.length - skipped));
    var detail = '';
    if(skipped){
      try{
        var raw = rr.headers.get('X-Skipped-Detail') || '[]';
        detail = (JSON.parse(decodeURIComponent(raw)) || []).slice(0, 2).join('；');
      }catch(e){}
    }
    saveBlob(await rr.blob(), '简历原件打包.zip');
    toast('已导出 ' + got + ' 份简历'
          + (skipped ? ('；' + skipped + ' 份跳过：' + detail) : ''));
    return;
  }
  if(act === 'year'){
    var sel = document.getElementById('archYear');
    var y = parseInt((sel && (sel.dataset.year || sel.value)) || new Date().getFullYear(), 10);
    if(!y || y < 1900 || y > 2200) y = new Date().getFullYear();
    var ok3 = await askConfirm({title: '按年归档', okText: '归档',
      body: '把「最后一条投递早于 ' + y + ' 年」的档案整批归档（当前还在人才库里的）。' +
            '\n\n归档后在「归档」页可见，可随时取消归档或彻底删除。继续？'});
    if(!ok3) return;
    var r3 = await api('/api/candidates/archive-batch', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({before_year: y, archived: true})});
    if(r3.__err){ toast(r3.__err); return; }
    toast('已归档 ' + (r3.changed || 0) + ' 人（按 ' + y + ' 年以前筛选命中 ' +
          (r3.picked_by_year || 0) + ' 人）' +
          (r3.ended_applications ? ('；结束 ' + r3.ended_applications + ' 条进行中的投递') : ''));
    loadArch();
  }
}

/* ============================================================
   系统配置（分组照搬原版：模型与密钥 / 发信配置 / 收信配置 / 面试字典 /
   模板管理 / 数据存放 / 运行环境）
   ⚠️ 密钥类字段一律"留空 = 不修改"，不回显明文。
   ============================================================ */
var CFG = {};
function cfgRow(label, ctrl){
  return '<div class="crow"><label>' + esc(label) + '</label>' + ctrl + '</div>';
}
/* 索引状态一行说完：用哪个模型、多少维、覆盖多少人、服务通不通。
   维度跟着模型变，混用会算错——所以这里必须把"实际在用哪个"摆出来，
   不能只显示配置里的模型名。 */
function embIndexText(idx){
  if(!idx || !idx.index_model) return '未建索引';
  var s = idx.index_model;
  if(idx.dim) s += ' · ' + idx.dim + ' 维';
  if(idx.people) s += ' · ' + idx.people + ' 人';
  if(idx.reachable === false) s += ' · 服务不可达（已退化为哈希向量）';
  return s;
}
/* ---------- 配置项：**现值与输入框分开** ----------
   业务方 2026-10-20 14:2x 指出：把输入框直接摆在现值旁边，HR 想"看一眼"就会手滑改掉。
   现在默认**只显示现值 + 一个「改」按钮**，点「改」才展开输入框；
   展开后这一行是**独占的一行**（不是挤在现值右边），改完点「保存」提交该组。 */
var CFG_VALUES = {};      /* 草稿：字段 id → 新值 */
var CFG_GROUP = {
  mcBaseUrl:'model', mcModel:'model', mcKey:'model',
  embEnabled:'embed', embBaseUrl:'embed', embModel:'embed',
  cfgSmtpHost:'smtp', cfgSmtpPort:'smtp', cfgSmtpSsl:'smtp', cfgSmtpUser:'smtp',
  cfgSmtpFrom:'smtp', cfgSmtpPwd:'smtp',
  cfgMbMode:'mb', cfgMbHost:'mb', cfgMbPort:'mb', cfgMbSsl:'mb', cfgMbUser:'mb',
  cfgMbFolder:'mb', cfgMbPwd:'mb', cfgEmlDir:'mb', cfgFolderDir:'mb',
  cfgUnits:'dict', cfgRooms:'dict', cfgSlots:'dict', cfgModes:'dict',
  cfgContacts:'dict', cfgMeetNo:'dict'
};
function cfgEditor(id, val, ph, type, isPwd, boolVal, options){
  if(type === 'bool'){
    return '<label class="ck"><input class="ced" type="checkbox" id="cedv_' + id + '"' +
      (boolVal ? ' checked' : '') + '> 启用</label>';
  }
  if(type === 'select'){
    return '<select class="ced" id="cedv_' + id + '">' + (options || []).map(function(p){
      return '<option value="' + esc(p[0]) + '"' + (String(val) === String(p[0]) ? ' selected' : '') +
        '>' + esc(p[1]) + '</option>';
    }).join('') + '</select>';
  }
  if(type === 'textarea'){
    return '<textarea class="ced" id="cedv_' + id + '" rows="4" placeholder="' + esc(ph || '') + '">' +
      esc(val) + '</textarea>';
  }
  /* ⚠️ 密码框**绝不预填 value**（2026-10-20 修复）：
     服务端按红线只回掩码（`key_masked` / "已配置"，`server.py:3948`），
     完整密钥永不下发。把这个掩码当现值填进 value，保存时 `if(v(id))` 判定 truthy
     → 掩码被当成真密钥提交 → 覆盖 config/secrets.json，**且不可逆**。
     密钥只能出现在只读的「现值」文本里，输入框一律留空（placeholder 提示"留空=不修改"）。*/
  return '<input class="ced" id="cedv_' + id + '" type="' + (isPwd ? 'password' : (type || 'text')) + '"' +
    ' placeholder="' + esc(ph || '') + '"' + (isPwd ? '' : ' value="' + esc(val) + '"') + '>';
}
/* 一个配置项 = 一行：默认只显示**现值**，编辑控件就在同一行里但**藏着**
   （模块级「修改」按钮一按，整组的控件一起露出来）。
   为什么不再"每项一个改按钮"：25 个「改」按钮铺在页面上，视觉上就是一片按钮，
   反而看不出哪里是配置、哪里是操作。 */
function cfgEdit(id, label, cur, ph, type, isPwd, boolVal, options){
  var val = (cur === null || cur === undefined) ? '' : String(cur);
  var shown = (val === '') ? '<span class="cur none">未设置</span>' : '<span class="cur">' + esc(val) + '</span>';
  return '<div class="crow cfgrow"><label>' + esc(label) + '</label>' +
    '<div class="cval">' + shown + cfgEditor(id, val, ph, type, isPwd, boolVal, options) + '</div></div>';
}
/* 一个分组 = 一张卡，右上角**一个**「修改」按钮；进入编辑态后变成「保存 / 取消」 */
/* cfgGroup：一个配置分组。`readonly` 为真时**不显示「修改」按钮** ——
   只有只读文本的分组（像"简历来源与导入"）如果也挂一个「修改」，
   点进去会把现值藏起来、又拿不出输入框顶上 → **整块空白**。 */
function cfgGroup(title, gid, rows, extra, readonly){
  return '<div class="sec cfgsec" data-g="' + gid + '">' +
    '<h3>' + esc(title) +
      (readonly ? '' : '<span class="cfgacts">' +
        '<button class="btn sm" data-cfg-g="edit">修改</button>' +
        '<button class="btn primary sm" data-cfg-g="save" style="display:none">保存</button>' +
        '<button class="btn sm" data-cfg-g="cancel" style="display:none">取消</button>' +
      '</span>') +
    '</h3>' +
    rows + (extra || '') + '</div>';
}
function cfgGroupEdit(gid, act){
  var sec = document.querySelector('.cfgsec[data-g="' + gid + '"]');
  if(!sec) return;
  /* ⚠️ 顺序：**先切 editing 类，再刷新按钮显隐**。
     反过来（先算按钮再切类）的话，按钮显隐是按旧的 editing 值算的 → 点「修改」后
     「修改」还亮着、「保存/取消」不出现。 */
  if(act === 'edit') sec.classList.add('editing');
  else if(act === 'cancel') sec.classList.remove('editing');
  var editing = sec.classList.contains('editing');
  Array.prototype.forEach.call(sec.querySelectorAll('[data-cfg-g]'), function(b){
    var a = b.dataset.cfgG;
    b.style.display = (a === 'edit') ? (editing ? 'none' : '') : (editing ? '' : 'none');
  });
  if(act === 'save'){
    if(gid === 'settings'){ saveSettings(); }
    else sysAct(gid);
    /* 存完重画一次：让只读区显示新值、退出编辑态 */
    setTimeout(initSys, 400);
  }
}
function cfgInput(id, val, ph, type, w){
  return '<input id="' + id + '" type="' + (type || 'text') + '"' +
    (w ? ' style="width:' + w + 'px"' : '') +
    ' placeholder="' + esc(ph || '') + '" value="' + esc(val == null ? '' : val) + '">';
}
function cfgText(id, val, ph, rows){
  return '<textarea id="' + id + '" rows="' + (rows || 4) + '" placeholder="' + esc(ph || '') + '">' +
    esc(val == null ? '' : val) + '</textarea>';
}
/* ============================================================
   账号管理（v3.0.3）
   ============================================================ */
var USERS = null;

async function initUsers(){
  var box = document.getElementById('usersView');
  box.innerHTML = '<div class="hint" style="padding:20px">读取账号…</div>';
  var j = await api('/api/users');
  if(j.__err){
    box.innerHTML = '<div class="hint" style="padding:20px">' + esc(j.__err) +
      '<br>（账号管理需要「系统管理员」角色）</div>';
    return;
  }
  USERS = j;
  renderUsers();
}

function renderUsers(){
  var box = document.getElementById('usersView');
  if(!box || !USERS) return;
  var roles = USERS.roles || [];
  var roleOf = {};
  roles.forEach(function(r){ roleOf[r.key] = r; });
  var rows = (USERS.items || []).map(function(u){
    var r = roleOf[u.role] || {label: u.role};
    var isMe = u.username === USERS.me;
    return '<tr' + (u.active ? '' : ' class="off"') + '>' +
      '<td><b>' + esc(u.display_name || u.username) + '</b>' +
        (isMe ? '<span class="utag me">当前登录</span>' : '') +
        (u.active ? '' : '<span class="utag off">已停用</span>') +
        '<div class="usub">' + esc(u.username) + '</div></td>' +
      '<td><select class="sel" onchange="userSetRole(\'' + esc(u.username) + '\', this.value)">' +
        roles.map(function(x){
          return '<option value="' + esc(x.key) + '"' +
            (x.key === u.role ? ' selected' : '') + '>' + esc(x.label) + '</option>';
        }).join('') + '</select></td>' +
      '<td class="usub">' + esc(r.desc || '') + '</td>' +
      '<td style="white-space:nowrap">' +
        '<button class="btn" onclick="userResetPassword(\'' + esc(u.username) + '\')">重置口令</button> ' +
        (u.active
          ? (isMe ? '' : '<button class="btn btn-danger" onclick="userSetActive(\'' +
              esc(u.username) + '\', false)">停用</button>')
          : '<button class="btn" onclick="userSetActive(\'' + esc(u.username) +
              '\', true)">启用</button>') +
      '</td></tr>';
  }).join('');

  var matrix = roles.map(function(r){
    return '<tr><td><b>' + esc(r.label) + '</b><div class="usub">' + esc(r.desc || '') +
      '</div></td><td>' +
      Object.keys(USERS.perm_labels || {}).map(function(p){
        var has = (r.permissions || []).indexOf(p) >= 0;
        return '<span class="pmask' + (has ? ' on' : '') + '" title="' +
          esc(USERS.perm_labels[p]) + '">' + (has ? '✓' : '·') + '</span>';
      }).join('') + '</td></tr>';
  }).join('');

  box.innerHTML =
    '<div class="uwrap">' +
      '<div class="uhead"><h3>账号</h3>' +
        '<button class="btn primary" onclick="userCreate()">新建账号</button></div>' +
      '<table class="utable"><thead><tr>' +
        '<th>账号</th><th>角色</th><th>说明</th><th>操作</th></tr></thead>' +
        '<tbody>' + (rows || '<tr><td colspan="4" class="hint">还没有账号</td></tr>') +
        '</tbody></table>' +
      '<div class="uhead" style="margin-top:26px"><h3>角色权限</h3>' +
        '<span class="usub">✓ = 该角色拥有此权限</span></div>' +
      /* 权限矩阵：表头用**竖排**（每个权限两字一行），13 列才排得下。
         横排会被挤成"查看查看使用让助…"这种没法读的东西。 */
      '<table class="utable pmtable"><thead><tr><th style="width:190px">角色</th><td>' +
        Object.keys(USERS.perm_labels || {}).map(function(p){
          var lab = USERS.perm_labels[p];
          return '<span class="pmcol" title="' + esc(lab) + '">' +
            esc(lab.slice(0, 2)) + '</span>';
        }).join('') + '</td></tr></thead><tbody>' + matrix + '</tbody></table>' +
      '<div class="hint" style="margin-top:16px">口令至少 ' + (USERS.min_password_len || 8) +
        ' 位。修改角色或停用账号会**立即清除该账号的登录状态**。</div>' +
    '</div>';
}

async function userCreate(){
  var name = await askText('新建账号', '', '账号名（登录用，不能再改）');
  if(!name || name === true) return;
  var pw = await askText('设置初始口令', '', '至少 8 位');
  if(!pw || pw === true) return;
  var role = await askText('角色（' +
    (USERS.roles || []).map(function(r){ return r.key + '=' + r.label; }).join(' / ') +
    '）', 'hr', 'hr / admin / viewer');
  if(!role || role === true) return;
  var r = await api('/api/users', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: String(name).trim(), password: String(pw),
                          role: String(role).trim(), display_name: ''})});
  if(r.__err){ toast('新建失败：' + r.__err, 'danger'); return; }
  toast('已新建账号：' + name);
  initUsers();
}

async function userSetRole(username, role){
  var r = await api('/api/users/' + encodeURIComponent(username),
    {method:'PATCH', headers:{'Content-Type':'application/json'},
     body: JSON.stringify({role: role})});
  if(r.__err){ toast('修改失败：' + r.__err, 'danger'); initUsers(); return; }
  toast('已修改「' + username + '」的角色');
  initUsers();
}

async function userSetActive(username, active){
  var r = await api('/api/users/' + encodeURIComponent(username),
    {method:'PATCH', headers:{'Content-Type':'application/json'},
     body: JSON.stringify({active: active})});
  if(r.__err){ toast('操作失败：' + r.__err, 'danger'); initUsers(); return; }
  toast((active ? '已启用 ' : '已停用 ') + username);
  initUsers();
}

async function userResetPassword(username){
  var pw = await askText('重置「' + username + '」的口令', '', '至少 8 位');
  if(!pw || pw === true) return;
  var r = await api('/api/users/' + encodeURIComponent(username) + '/password',
    {method:'POST', headers:{'Content-Type':'application/json'},
     body: JSON.stringify({new_password: String(pw)})});
  if(r.__err){ toast('重置失败：' + r.__err, 'danger'); return; }
  toast('已重置口令。该账号需要重新登录。');
}

async function initSys(){
  var box = document.getElementById('sysView');
  box.innerHTML = '<div class="hint" style="padding:20px">读取配置…</div>';
  var r = await Promise.all([
    api('/api/model-config'), api('/api/mail/smtp'), api('/api/mailbox/config'),
    api('/api/mailbox/presets'), api('/api/interview-dict'), api('/api/mail/templates'),
    api('/api/embedding-config')
  ]);
  CFG = {model: r[0] || {}, smtp: r[1] || {}, mailbox: r[2] || {},
         presets: (r[3] || {}).presets || [], dict: r[4] || {}, tpl: (r[5] || {}).items || [],
         embed: r[6] || {}};
  var m = CFG.model, sb = CFG.smtp, mb = CFG.mailbox, dc = CFG.dict;
  var em = CFG.embed, emIdx = em.index || {};
  var presetOpts = ['<option value="">（选一个邮箱预设）</option>'].concat(
    CFG.presets.map(function(p){
      return '<option value="' + esc(p.host + '|' + p.port + '|' + (p.ssl ? '1' : '0')) + '">' +
        esc(p.label) + '（' + esc(p.host) + '）</option>';
    })).join('');
  /* SMTP 预设：`/api/mail/smtp` 的 GET **本来就返回 presets**
     （`server.py:5023`），v2 之前只是没用上 —— 填错端口/SSL 正是"配了没反应"
     最常见的原因，直接给现成的（与收信预设同一套做法）。 */
  var smtpPresetOpts = ['<option value="">（选一个发信预设）</option>'].concat(
    ((sb.presets || []).map(function(p){
      return '<option value="' + esc(p.host + '|' + p.port + '|' + (p.ssl ? '1' : '0')) + '">' +
        esc(p.label) + '（' + esc(p.host + ':' + p.port) + '）</option>';
    }))).join('');
  var dictLines = function(a){ return (a || []).join('\n'); };
  var contactLines = (dc.contacts || []).map(function(c){
    /* 分隔符与解析侧保持一致（竖线）：用「·」时部门名里带「·」会解析错位，
       而只填姓名会被当成"部门=整串"。竖线在部门/姓名里不会出现。 */
    return [c.dept, c.name, c.phone].filter(Boolean).join(' | ');
  }).join('\n');

  box.innerHTML =
    '<div class="dhead" style="margin-bottom:4px"><div style="min-width:0">' +
      '<div class="dname">系统配置</div>' +
    '</div></div>' +

    '<div class="sec cfgsec" data-g="model"><h3>模型与密钥' + '<span class="cfgacts">' + '<button class="btn sm" data-cfg-g="edit">修改</button>' + '<button class="btn primary sm" data-cfg-g="save" style="display:none">保存</button>' + '<button class="btn sm" data-cfg-g="cancel" style="display:none">取消</button>' + '</span></h3>' +
      cfgEdit('mcBaseUrl', '接口地址', m.base_url, 'https://…/compatible-mode/v1', 'text') +
      cfgEdit('mcModel', '模型名', m.model, '', 'text') +
      cfgEdit('mcKey', 'API Key', m.key_set ? ('已配置（' + m.key_masked + '）') : '未配置',
              m.key_set ? '留空 = 不修改' : '', 'password', true) +
    '</div>' +

    /* 向量模型（v4.0.1 新增）：以前只能在 config/embedding.json 里手改，
       HR 拿不到入口。API Key 不在这里收 —— 与对话模型共用 secrets.json 那一份。 */
    '<div class="sec cfgsec" data-g="embed"><h3>向量模型' + '<span class="cfgacts">' + '<button class="btn sm" data-cfg-g="edit">修改</button>' + '<button class="btn primary sm" data-cfg-g="save" style="display:none">保存</button>' + '<button class="btn sm" data-cfg-g="cancel" style="display:none">取消</button>' + '</span></h3>' +
      cfgEdit('embEnabled', '启用', em.enabled ? '1' : '0', '', 'select', false, null,
              [['1', '开'], ['0', '关']]) +
      cfgEdit('embBaseUrl', '接口地址', em.base_url, 'https://…/compatible-mode/v1', 'text') +
      cfgEdit('embModel', '模型名', em.model, '', 'text') +
      '<div class="crow cfgrow"><label>索引</label><div class="cval">' +
        '<span class="cur">' + esc(embIndexText(emIdx)) + '</span>' +
        '<button class="btn sm" data-cfg="reindex">重建索引</button></div></div>' +
    '</div>' +

    '<div class="sec cfgsec" data-g="smtp"><h3>发信配置（SMTP）' + '<span class="cfgacts">' + '<button class="btn sm" data-cfg-g="edit">修改</button>' + '<button class="btn primary sm" data-cfg-g="save" style="display:none">保存</button>' + '<button class="btn sm" data-cfg-g="cancel" style="display:none">取消</button>' + '</span></h3>' +
      cfgRow('SMTP 预设',
        '<select class="ced" id="cfgSmtpPreset" style="width:320px">' + smtpPresetOpts + '</select>') +
      cfgEdit('cfgSmtpHost', '服务器', sb.host, 'smtp.exmail.qq.com', 'text') +
      cfgEdit('cfgSmtpPort', '端口', sb.port, '465', 'number') +
      cfgEdit('cfgSmtpSsl', '加密', sb.ssl ? 'SSL' : 'STARTTLS', 'SSL / STARTTLS', 'bool', false, sb.ssl) +
      cfgEdit('cfgSmtpUser', '账号', sb.user, '发件邮箱', 'text') +
      cfgEdit('cfgSmtpFrom', '显示名', sb.from_name, '如 西北有色金属研究院', 'text') +
      cfgEdit('cfgSmtpPwd', '授权码', sb.password_set ? '已配置' : '未配置',
              sb.password_set ? '留空 = 不修改' : '输入后保存', 'password', true) +
      '<div style="margin-top:10px"><button class="btn" data-cfg="smtpTest">测试发信</button>' +
        '<span class="msgline busy" id="smtpMsg" style="display:none"></span></div>' +
    '</div>' +

    '<div class="sec cfgsec" data-g="mb"><h3>收信配置' + '<span class="cfgacts">' + '<button class="btn sm" data-cfg-g="edit">修改</button>' + '<button class="btn primary sm" data-cfg-g="save" style="display:none">保存</button>' + '<button class="btn sm" data-cfg-g="cancel" style="display:none">取消</button>' + '</span></h3>' +
      cfgEdit('cfgMbMode', '方式', mb.mode, 'imap / eml / off', 'select',
              false, null, [['imap','IMAP 收信'],['eml','收 EML 文件'],['off','不收信']]) +
      cfgRow('邮箱预设', '<select class="ced" id="cfgPreset" style="width:320px">' + presetOpts + '</select>') +
      cfgEdit('cfgMbHost', 'IMAP 服务器', mb.imap_host, 'imap.exmail.qq.com', 'text') +
      cfgEdit('cfgMbPort', '端口', mb.imap_port, '993', 'number') +
      cfgEdit('cfgMbSsl', '加密', mb.imap_ssl ? 'SSL' : 'STARTTLS', 'SSL / STARTTLS', 'bool', false, mb.imap_ssl) +
      cfgEdit('cfgMbUser', '账号', mb.imap_user, '收件邮箱', 'text') +
      cfgEdit('cfgMbFolder', '文件夹', mb.imap_folder, 'INBOX', 'text') +
      cfgEdit('cfgMbPwd', '授权码', mb.password_set ? '已配置' : '未配置',
              mb.password_set ? '留空 = 不修改' : '输入后保存', 'password', true) +
      cfgEdit('cfgEmlDir', 'EML 目录', mb.eml_dir, 'data/mail_in', 'text') +
cfgEdit('cfgFolderDir', '简历目录', mb.folder_dir, 'data/resumes', 'text') +
      /* 这三项原来既不展示也不提交 —— 在 v2 里**永远改不了**，而后端是支持的
         （`MailboxCfgReq`：`attachment_ext` / `max_attachment_mb` /
         `same_job_reapply_days`，`server.py:4394-4396`），GET 也已经回传
         （`server.py:4411-4413`）。附件白名单为空时后端还会警告
         "任何附件都不会被收进来"（`server.py:4460-4462`）。 */
      cfgEdit('cfgMbAttExt', '附件白名单', (mb.attachment_ext || []).join(','),
              '.pdf,.docx,.doc,.txt,.md', 'text') +
      cfgEdit('cfgMbMaxMb', '单附件上限(MB)', mb.max_attachment_mb, '20', 'number') +
      cfgEdit('cfgMbReapply', '同岗位重投窗口(天)', mb.same_job_reapply_days,
              '', 'number') +
      '<div style="margin-top:10px"><button class="btn" data-cfg="mbTest">测试收信</button>' +
        '<span class="msgline busy" id="mbMsg" style="display:none"></span></div>' +
      '<div style="margin-top:8px"><button class="btn" onclick="previewMailbox()">预览收信</button>' +
        '<span class="msgline busy" id="mbPvMsg" style="display:none"></span></div>' +
      '<div id="mbPvBox" style="display:none;margin-top:8px"></div>' +
    '</div>' +

    '<div class="sec cfgsec" data-g="dict"><h3>面试字典' + '<span class="cfgacts">' + '<button class="btn sm" data-cfg-g="edit">修改</button>' + '<button class="btn primary sm" data-cfg-g="save" style="display:none">保存</button>' + '<button class="btn sm" data-cfg-g="cancel" style="display:none">取消</button>' + '</span></h3>' +
      dictItemsRow('units', '面试单位', dc.units) +
      cfgEdit('cfgUnits', '面试单位', dictLines(dc.units), '一行一个', 'textarea') +
      dictItemsRow('rooms', '会议室', dc.rooms) +
      cfgEdit('cfgRooms', '会议室', dictLines(dc.rooms), '一行一个', 'textarea') +
      dictItemsRow('slots', '面试时段', dc.slots) +
      cfgEdit('cfgSlots', '面试时段', dictLines(dc.slots), '一行一个', 'textarea') +
      dictItemsRow('modes', '面试方式', dc.modes) +
      cfgEdit('cfgModes', '面试方式', dictLines(dc.modes), '一行一个', 'textarea') +
      dictItemsRow('contacts', '联系人', (dc.contacts || []).map(function(c){
        return [c.dept, c.name, c.phone].filter(Boolean).join(' · '); })) +
      cfgEdit('cfgContacts', '联系人', contactLines, '部门 | 姓名 | 电话，一行一个', 'textarea') +
      /* 原来的「会议平台」输入框已删：后端字典保存只认 `meeting_no`
         （`server.py:732-733`），`meeting_platform` 不在白名单里，
         提交后**静默丢弃** —— 一个填了永远没反应的框，不如没有。 */
      cfgEdit('cfgMeetNo', '会议号', dc.meeting_no, '线上面试的会议号或入会链接', 'text') +
    '</div>' +

    '<div class="sec"><h3 class="ha">模板管理' +
      '<span class="h3act"><button class="btn sm" data-tpl-new="1">新建模板</button></span>' +
      '</h3>' +
      '<div id="cfgTplList">' + CFG.tpl.map(function(t){
        return '<div class="doc"><span class="doc-n">' + esc(t.name || '') +
          (t.scene ? '（' + esc(t.scene) + '）' : '') + '</span>' +
          '<span class="doc-a">' +
            '<button class="btn sm" data-tpl-edit="' + t.id + '">编辑</button> ' +
            '<button class="btn sm" data-tpl-del="' + t.id + '">删除</button>' +
          '</span></div>';
      }).join('') + '</div>' +
      (CFG.tpl.length ? '' : '<div class="hint">还没有模板</div>') +
      '<div style="margin-top:10px"><button class="btn" data-cfg="newTpl">新建模板</button></div>' +
    '</div>' +

    cfgGroup('简历来源与导入', 'sources',
      '<div class="crow"><label>来源文件夹</label><div class="cval">' +
        '<span class="cur" id="srcPath">读取中…</span></div></div>' +
      '<div class="crow"><label>待导入</label><div class="cval">' +
        '<span class="cur" id="srcFiles">—</span></div></div>' +
      '<div id="srcFileList" style="margin:2px 0 6px"></div>' +
      /* 逐文件操作（业务方 2026-10-21）：勾选后可单独移除，
         回收站里也能逐个恢复 —— 原来只能"整个目录全移 / 全部恢复"，粒度太粗。 */
      '<div style="margin:2px 0 8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
        '<label class="ck2"><input type="checkbox" id="srcChkAll"> 全选</label>' +
        '<button class="btn sm" onclick="srcRemoveChecked()">移除勾选</button>' +
        '<span class="hint" id="srcChkInfo"></span></div>' +
      '<div class="crow"><label>回收站</label><div class="cval">' +
        '<span class="cur" id="srcRecycle">—</span>' +
        '<button class="btn sm" data-src="recycle">全部恢复</button></div></div>' +
      '<div id="srcRecList" style="margin:2px 0 6px"></div>',
      '', true) +

    cfgGroup('运行设置', 'settings',
      '<div class="crow"><label>管道超期</label><div class="cval">' +
        '<span class="cur" id="setOverdue">—</span>' +
        '<input class="ced" id="cedv_pipeline_overdue_days" type="number" min="1" ' +
          'style="width:96px;height:30px">' +
        '<span class="hint">停留多少天算超期（管道里标红）</span></div></div>' +
      '<div class="crow"><label>自动分析</label><div class="cval">' +
        '<span class="cur" id="setAuto">—</span>' +
        '<label class="ck"><input class="ced" type="checkbox" id="cedv_auto_insight_on_ingest">' +
        ' 入库后自动生成分析结论</label></div></div>') +

    '<div class="sec"><h3>数据存放与运行环境</h3><dl class="kv" id="cfgEnv"></dl></div>';

  Array.prototype.forEach.call(box.querySelectorAll('[data-cfg]'), function(b){
    b.addEventListener('click', function(){ sysAct(b.dataset.cfg); });
  });
  Array.prototype.forEach.call(box.querySelectorAll('[data-src]'), function(b){
    b.addEventListener('click', function(){ srcAct(b.dataset.src); });
  });
  Array.prototype.forEach.call(box.querySelectorAll('[data-cfg-g]'), function(b){
    b.addEventListener('click', function(){
      var g = b.closest('.cfgsec');
      cfgGroupEdit(g ? g.dataset.g : '', b.dataset.cfgG);
    });
  });
  Array.prototype.forEach.call(box.querySelectorAll('[data-tpl-del]'), function(b){
    b.addEventListener('click', function(){ delTpl(Number(b.dataset.tplDel)); });
  });
  Array.prototype.forEach.call(box.querySelectorAll('[data-tpl-edit]'), function(b){
    b.addEventListener('click', function(){ editTpl(Number(b.dataset.tplEdit)); });
  });
  var tn = box.querySelector('[data-tpl-new]');
  if(tn) tn.addEventListener('click', function(){ editTpl(0); });
  var ps = document.getElementById('cfgPreset');
  var sp = document.getElementById('cfgSmtpPreset');
  if(sp) sp.addEventListener('change', function(){
    var v2 = sp.value;
    if(!v2) return;
    var p2 = v2.split('|');
    document.getElementById('cfgSmtpHost').value = p2[0] || '';
    document.getElementById('cfgSmtpPort').value = p2[1] || '';
    document.getElementById('cfgSmtpSsl').checked = p2[2] === '1';
    toast('已填好发信服务器与端口；记得填授权码（不是登录密码）', 'ok');
  });
  if(ps) ps.addEventListener('change', function(){
    var v = ps.value;
    if(!v) return;
    var parts = v.split('|');
    document.getElementById('cfgMbHost').value = parts[0] || '';
    document.getElementById('cfgMbPort').value = parts[1] || '';
    document.getElementById('cfgMbSsl').checked = parts[2] === '1';
    /* 选了预设却仍留在「收 EML 文件」上，等于"配了没反应"——收的还是本地目录。
       原版选完预设会把方式置成 imap 并 toast 说明（`ui.py:6259-6272`）。 */
    var ms = document.getElementById('cedv_cfgMbMode');
    if(ms && ms.value !== 'imap'){
      ms.value = 'imap';
      toast('已按预设填好服务器；方式已自动切到「IMAP 收信」', 'ok');
    }
  });
  loadEnv(); loadSources(); loadSettings();
  killAutofill();
}
async function loadEnv(){
  var el = document.getElementById('cfgEnv');
  if(!el) return;
  var st = await api('/api/stats');
  if(st.__err){ el.innerHTML = '<dt>状态</dt><dd>读取失败</dd>'; return; }
  var rows = [
    ['候选人数', st.people + '（含归档 ' + st.people_total + '）'],
    ['投递数', st.applications],
    ['简历附件', st.documents],
    ['在招岗位', st.jobs_open + ' / ' + st.jobs_total],
    ['技能条目', st.skills],
    ['语义向量', st.embeddings],
    ['待确认提案', st.proposals_pending]
  ];
  el.innerHTML = rows.map(function(r){
    return '<dt>' + esc(r[0]) + '</dt><dd class="num">' + esc(String(r[1])) + '</dd>';
  }).join('');
}
async function sysAct(act){
  var box = document.getElementById('sysView');
  /* 取值优先级：草稿（行内改过）> 编辑框里的当前值 > 空 */
  function v(id){
    if(Object.prototype.hasOwnProperty.call(CFG_VALUES, id)) return String(CFG_VALUES[id]).trim();
    var e = document.getElementById('cedv_' + id) || document.getElementById(id);
    if(!e) return '';
    if(e.type === 'checkbox') return e.checked ? '1' : '0';
    return (e.value || '').trim();
  }
  if(act === 'model'){
    var body = {base_url: v('mcBaseUrl'), model: v('mcModel')};
    if(v('mcKey')) body.api_key = v('mcKey');
    var r = await api('/api/model-config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    toast(r.__err ? ('保存失败：' + r.__err) : '模型配置已保存（已改 ' + (r.changed || []).length + ' 项）');
    return;
  }
  if(act === 'embed'){
    var be = {base_url: v('embBaseUrl'), model: v('embModel'),
              enabled: v('embEnabled') === '1'};
    var re = await api('/api/embedding-config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(be)});
    if(re.__err){ toast('保存失败：' + re.__err, 'danger'); return; }
    toast((re.changed || []).length
      ? '向量模型已保存，点「重建索引」生效'
      : '没有修改', (re.changed || []).length ? 'warn' : 'ok');
    return;
  }
  if(act === 'reindex'){
    toast('重建索引中…（人数多时需要一会儿）', 'warn');
    var ri = await api('/api/search/index', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({force: true})});
    if(ri.__err){ toast('重建失败：' + ri.__err, 'danger'); return; }
    toast('索引已重建：' + (ri.indexed || 0) + ' 人 / 模型 ' + (ri.model || '—'), 'ok');
    initSys();
    return;
  }
  if(act === 'smtpTest'){
    var m0 = document.getElementById('smtpMsg');
    if(m0){ m0.style.display = ''; m0.className = 'msgline busy'; m0.textContent = '测试中…'; }
    var tt = await api('/api/mail/test-smtp', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: '{}'});
    if(m0){ m0.className = tt.__err ? 'msgline bad' : 'msgline';
             m0.textContent = tt.__err ? ('✗ ' + tt.__err) : ('✓ ' + (tt.message || '发信配置可用')); }
    return;
  }
  if(act === 'smtp' || act === 'smtpTest'){
    if(act === 'smtp'){
      var b1 = {host: v('cfgSmtpHost'), port: Number(v('cfgSmtpPort') || 465),
                ssl: v('cfgSmtpSsl') === '1',
                user: v('cfgSmtpUser'), from_name: v('cfgSmtpFrom')};
      if(v('cfgSmtpPwd')) b1.password = v('cfgSmtpPwd');
      var r1 = await api('/api/mail/smtp', {method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify(b1)});
      toast(r1.__err ? ('保存失败：' + r1.__err) : '发信配置已保存');
      return;
    }
    var t1 = await api('/api/mail/test-smtp', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: '{}'});
    toast(t1.__err ? ('✗ ' + t1.__err) : ('✓ ' + (t1.message || '发信配置可用')));
    return;
  }
  if(act === 'mbTest'){
    var m1 = document.getElementById('mbMsg');
    if(m1){ m1.style.display = ''; m1.className = 'msgline busy'; m1.textContent = '测试中…'; }
    var tm = await api('/api/mailbox/test', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: '{}'});
    if(m1){ m1.className = tm.__err ? 'msgline bad' : 'msgline';
             m1.textContent = tm.__err ? ('✗ ' + tm.__err) : ('✓ ' + (tm.message || '收信配置可用')); }
    return;
  }
  if(act === 'mb' || act === 'mbTest'){
    if(act === 'mb'){
      /* 空输入框一律发 null（= 不改动），不发空串：v4 起这几项默认就是空的，
         发空串会在配置文件里写下 ""，把有效缺省值冲掉。 */
      var b2 = {mode: v('cfgMbMode'),
                eml_dir: v('cfgEmlDir') || null,
                folder_dir: v('cfgFolderDir') || null,
                imap_host: v('cfgMbHost') || null,
                imap_port: v('cfgMbPort') ? Number(v('cfgMbPort')) : null,
                imap_ssl: v('cfgMbSsl') === '1',
                imap_user: v('cfgMbUser') || null,
                imap_folder: v('cfgMbFolder') || null};
      if(v('cfgMbPwd')) b2.password = v('cfgMbPwd');
      /* 附件白名单 / 单附件上限 / 同岗位重投窗口：原来**完全不提交**，
         于是这三项在 v2 里既改不了、也看不到现值。后端是支持的
         （`server.py:4394-4396`）。只提交填了的项，避免空值把现值清掉。 */
      var ext = v('cfgMbAttExt').split(/[,，\s]+/).map(function(s){ return s.trim(); })
        .filter(Boolean);
      if(ext.length) b2.attachment_ext = ext;
      if(v('cfgMbMaxMb')) b2.max_attachment_mb = Number(v('cfgMbMaxMb'));
      if(v('cfgMbReapply')) b2.same_job_reapply_days = Number(v('cfgMbReapply'));
      var r2 = await api('/api/mailbox/config', {method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify(b2)});
      if(r2.__err){ toast('保存失败：' + r2.__err, 'danger'); return; }
      /* 后端会算出这处配置的"待确认项"（如"附件白名单为空，任何附件都不会被收进来"，
         `server.py:4454-4463`）。原来这里只报"已保存"，配错了毫无提示。 */
      if((r2.warnings || []).length){
        toast('已保存，但有几处需要确认：' + r2.warnings.join('；'), 'warn');
      }else{
        toast('收信配置已保存', 'ok');
      }
      return;
    }
    var t2 = await api('/api/mailbox/test', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: '{}'});
    toast(t2.__err ? ('✗ ' + t2.__err) : ('✓ ' + (t2.message || '收信配置可用')));
    return;
  }
  if(act === 'dict'){
    var lines = function(id){
      return v(id).split('\n').map(function(s){ return s.trim(); }).filter(Boolean);
    };
    var contacts = lines('cfgContacts').map(function(s){
      /* ⚠️ 原来按中文间隔号「·」切分：部门名里带「·」会整体错位，
         而**只填姓名**（HR 只想记一个人）会被解析成 dept=整串、name=整串，
         于是写邮件带出来的联系人信息是错的。
         现在改用竖线作分隔（部门/姓名里不会出现），并**按段数**判断：
         1 段=只有姓名，2 段=部门+姓名，3 段=部门+姓名+电话。 */
      var p = s.split('|').map(function(x){ return x.trim(); });
      if(p.length >= 3) return {dept: p[0], name: p[1], phone: p[2]};
      if(p.length === 2) return {dept: p[0], name: p[1], phone: ''};
      return {dept: '', name: p[0] || '', phone: ''};
    });
    var b3 = {units: lines('cfgUnits'), rooms: lines('cfgRooms'), slots: lines('cfgSlots'),
              modes: lines('cfgModes'), contacts: contacts,
              meeting_no: v('cfgMeetNo')};
    var r3 = await api('/api/interview-dict', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(b3)});
    toast(r3.__err ? ('保存失败：' + r3.__err) : '面试字典已保存');
    return;
  }
  if(act === 'newTpl'){
    var name = await askText('新模板名称', '', '例如 面试邀请（院标准版式）');
    if(!name || name === true) return;
    var subject = await askText('邮件主题', '', '例如 西北有色金属研究院面试邀请');
    if(!subject || subject === true) return;
    var body2 = await askText('正文（可用 {姓名} {岗位} 这样的变量）', '', '正文');
    if(body2 === true) body2 = '';
    var r4 = await api('/api/mail/templates', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: name, subject: subject, body: body2})});
    toast(r4.__err ? ('新建失败：' + r4.__err) : ('已新建模板：' + name));
    initSys();
  }
}
/* 编辑邮件模板：POST /api/mail/templates 带 id 就是更新
   （db.upsert_mail_template：传 id 走更新、不传走新建）。 */
async function editTpl(id){
  /* id 为空/0 = 新建（原来只能编辑已有模板，列表里连"新建"入口都没有）。 */
  var isNew = !id;
  var t = isNew ? {} : CFG.tpl.filter(function(x){ return x.id === id; })[0];
  if(!isNew && !t){ toast('模板不存在', 'warn'); return; }
  /* 套用宽体弹窗（富文本编辑器需要横向空间，560px 里正文只剩一条缝） */
  var dlgBox = document.querySelector('#mask .dlg');
  if(dlgBox) dlgBox.classList.add('wide');
  openProgDlg(isNew ? '新建模板' : ('编辑模板' + (t.name ? '：' + t.name : '')),
    '<div class="crow"><label>名称</label><div class="cinput">' +
      '<input class="ced" id="tplName" value="' + esc(t.name || '') + '"></div></div>' +
    '<div class="crow"><label>场景</label><div class="cinput">' +
      '<input class="ced" id="tplScene" value="' + esc(t.scene || '') + '"></div></div>' +
    /* 用途必须显式选（2026-10-28）：它决定"这封发出去之后系统回写什么"。
       按模板名猜是行不通的 —— 名字随时会改，改完就静默失效。
       默认「其他通知」= 不回写任何东西。 */
    '<div class="crow"><label>用途</label><div class="cinput">' +
      '<select class="ced" id="tplKind" style="width:280px">' +
        [['other', '其他通知（不回写档案）'],
         ['interview_invite', '面试邀约（回写面试单位/日期/时段/方式）'],
         ['offer', '录用通知（记录录用函时间并推进到已发放offer）']].map(function(o){
          return '<option value="' + o[0] + '"' +
            ((t.kind || 'other') === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
        }).join('') +
      '</select></div></div>' +
    '<div class="crow"><label>主题</label><div class="cinput">' +
      '<input class="ced" id="tplSubject" value="' + esc(t.subject || '') + '"></div></div>' +
    /* 正文改用**富文本编辑器**（业务方 2026-10-21：原版是富文本 + 表格工具条，
       v2 退化成 textarea，院里的标准模板基本都带表格，纯文本维护不了）。
       复用写邮件页那套 `.mtool` + `.richeditor`，命令走同一个 `fmt()`。 */
    '<div class="crow" style="align-items:flex-start"><label>正文</label><div class="cinput">' +
      '<div class="mtool">' +
        '<button type="button" class="btn sm" title="加粗" onclick="fmt(\'bold\',\'tplEditor\')"><b>B</b></button>' +
        '<button type="button" class="btn sm" title="斜体" onclick="fmt(\'italic\',\'tplEditor\')"><i>I</i></button>' +
        '<button type="button" class="btn sm" title="下划线" onclick="fmt(\'underline\',\'tplEditor\')"><u>U</u></button>' +
        '<button type="button" class="btn sm" title="项目符号" onclick="fmt(\'insertUnorderedList\',\'tplEditor\')">• 列表</button>' +
        '<button type="button" class="btn sm" title="插入表格" onclick="tplInsertTable()">▦ 表格</button>' +
        '<select class="sel" style="height:28px" onchange="tplInsertVar(this.value);this.value=\'\'">' +
          '<option value="">插入变量…</option>' +
          /* `MAIL_VARS` 是写邮件页初始化时才拉的；在「系统配置」里直接编辑模板时它可能还是空的，
             那样下拉里一个变量都没有。这里用内置清单兜底，保证随时能插入。 */
          ((MAIL_VARS.runtime || []).length
            ? (MAIL_VARS.runtime || []).map(function(v){ return v.key; })
            : ['姓名','学历','学校','专业','岗位','面试时间','面试时段','面试地点',
               '面试单位','面试方式','会议号','联系人','联系电话']
          ).map(function(k){
            return '<option value="' + esc(k) + '">' + esc(k) + '</option>';
          }).join('') +
        '</select>' +
      '</div>' +
      '<div class="richeditor" id="tplEditor" contenteditable="true" ' +
        'style="min-height:320px;font-size:14px">' + (t.body || '') + '</div>' +
    '</div></div>' +
    '<div class="hint" style="margin-top:6px">正文可直接排版（表格、加粗都行）；' +
      '用 {姓名} {学历} {面试时间} 这类变量，发信时会替换成真实值。</div>' +
    '<div class="bar" style="margin-top:16px;justify-content:flex-end">' +
      '<button class="btn primary" onclick="saveTpl(' + id + ')">保存</button></div>');
}
async function saveTpl(id){
  var g = function(x){ var e = document.getElementById(x); return e ? e.value : ''; };
  var name = g('tplName').trim();
  if(!name){ toast('模板名称不能为空'); return; }
  /* 正文现在在富文本编辑器里，取 innerHTML（原来是 textarea.value）。
     存进库的仍是 HTML —— 发信时按 body_html 下发，客户端按 HTML 渲染。 */
  var ed = document.getElementById('tplEditor');
  var r = await api('/api/mail/templates', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id: id, name: name, scene: g('tplScene').trim(),
                          kind: g('tplKind') || 'other',
                          subject: g('tplSubject'), body: ed ? ed.innerHTML : ''})});
  if(r.__err){ toast(r.__err); return; }
  closeProgDlg();
  toast('已保存模板：' + name);
  initSys();
}
async function delTpl(id){
  var ok = await askConfirm({title: '删除模板', danger: true, okText: '删除',
    body: '删除这个邮件模板？\n\n写邮件页的模板下拉里就不再有它了。'});
  if(!ok) return;
  var r = await api('/api/mail/templates/' + id + '/delete', {method:'POST'});
  toast(r.__err ? ('删除失败：' + r.__err) : '已删除');
  initSys();
}

/* ============================================================
   智能助手（左对话 / 右 工具清单 + 最近运行）
   口径照搬原版：回复按 Markdown 渲染；**工具轨迹默认折叠**，
   但有 [已拦截] 时默认展开（那是"这件事没做"的原因，HR 必须看见）。
   ============================================================ */
var CHAT = [], CHAT_HIST = [];
function mdLite(src){
  /* Markdown 渲染（2026-10-20 补齐）。原来这个函数只做标题/粗体/斜体/行内代码/单一列表，
     **模型做"全员对比"时输出的管道表格整块以原始 `|` 文本怼在页面上**
     —— 原版 v1.24.20 就修过这件事（`ui.py:822` 的注释里引着当时的用户反馈：
     "回复页面还是没有实现表格的正常渲染"），v2 重写时又丢了。
     这里按原版口径移植：先整体转义（防注入），再逐块处理，最后逐行组装。
     顺序不能反：先转义 → 再替换，反过来就会被注入。 */
  var s = esc(String(src == null ? '' : src));
  /* 代码块先占位，避免里面的标记被后续替换吃掉 */
  var blocks = [];
  s = s.replace(/```[ \t]*([A-Za-z0-9+#._-]*)\s*\n?([\s\S]*?)```/g, function(m, lang, code){
    blocks.push('<pre><code>' + code.replace(/^\n+|\n+$/g, '') + '</code></pre>');
    return '\n<pre>ZBLOCK' + (blocks.length - 1) + 'Z</pre>\n';
  });
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>')
       .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
       .replace(/^######[ \t]+(.+)$/gm, '<h4>$1</h4>')
       .replace(/^#####[ \t]+(.+)$/gm, '<h4>$1</h4>')
       .replace(/^####[ \t]+(.+)$/gm, '<h4>$1</h4>')
       .replace(/^###[ \t]+(.+)$/gm, '<h4>$1</h4>')
       .replace(/^##[ \t]+(.+)$/gm, '<h3>$1</h3>')
       .replace(/^#[ \t]+(.+)$/gm, '<h3>$1</h3>')
       .replace(/^&gt; (.*)$/gm, '<blockquote>$1</blockquote>')
       .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>');
  /* 链接：放开到任意协议与相对路径（模型很爱写站内锚点）；javascript: 由白名单挡掉 */
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function(m, txt, url){
    if(!/^(https?:\/\/|mailto:|\/|#|\.)/i.test(url)) return m;
    return '<a href="' + url + '" target="_blank" rel="noopener">' + txt + '</a>';
  });
  /* ---- 逐行组装：表格 / 列表 / 段落 ---- */
  var lines = s.split(/\n/);
  var out = [], liBuf = [], liTag = 'ul', paraBuf = [];
  var flushLi = function(){
    if(liBuf.length){ out.push('<' + liTag + '>' + liBuf.join('') + '</' + liTag + '>'); liBuf = []; }
  };
  var flushPara = function(){
    if(paraBuf.length){ out.push('<p>' + paraBuf.join('<br>') + '</p>'); paraBuf = []; }
  };
  /* 表格判据：**表头行 + 分隔行同时存在**才算表格。
     只看"有竖线"会误伤正文里的 "A | B"；分隔行才是确证。 */
  var isSepRow = function(t){ return /^\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?$/.test(t); };
  var splitRow = function(t){ return t.replace(/^\||\|$/g, '').split('|').map(function(c){ return c.trim(); }); };
  /* 单元格里的 &lt;br&gt; 是模型故意换行，esc 转义过，这里还原 */
  var cell = function(c){ return String(c).replace(/&lt;br\s*\/?&gt;/gi, '<br>'); };
  for(var i = 0; i < lines.length; i++){
    var t = lines[i].trim();
    if(t.indexOf('|') >= 0 && i + 1 < lines.length && isSepRow(lines[i + 1].trim())){
      flushLi(); flushPara();
      var head = splitRow(t);
      i += 1;
      var body = [];
      while(i + 1 < lines.length){
        var nxt = lines[i + 1].trim();
        if(!nxt || nxt.indexOf('|') < 0) break;
        body.push(splitRow(nxt));
        i += 1;
      }
      var cols = head.length;
      /* 列数对齐：缺列补空、多列截断（模型偶尔写歪，截断会串位） */
      var fit = function(r){
        var o = [];
        for(var k = 0; k < cols; k++) o.push(cell(r[k] == null ? '' : r[k]));
        return o;
      };
      var html = '<div class="mdtable-wrap"><table class="mdtable"><thead><tr>'
        + head.map(function(h){ return '<th>' + cell(h) + '</th>'; }).join('')
        + '</tr></thead><tbody>';
      body.forEach(function(r){
        html += '<tr>' + fit(r).map(function(c){ return '<td>' + c + '</td>'; }).join('') + '</tr>';
      });
      html += '</tbody></table></div>';
      out.push(html);
      continue;
    }
    if(!t){ flushLi(); flushPara(); continue; }
    if(/^[-*+][ \t]+/.test(t)){
      flushPara();
      if(liBuf.length && liTag !== 'ul') flushLi();
      liTag = 'ul';
      liBuf.push('<li>' + t.replace(/^[-*+][ \t]+/, '') + '</li>');
      continue;
    }
    if(/^\d+\.[ \t]+/.test(t)){
      flushPara();
      if(liBuf.length && liTag !== 'ol') flushLi();
      liTag = 'ol';
      liBuf.push('<li>' + t.replace(/^\d+\.[ \t]+/, '') + '</li>');
      continue;
    }
    if(/^<\/?(ul|ol|pre|h[1-4])\b/.test(t)){ flushLi(); flushPara(); out.push(t); continue; }
    flushLi();
    paraBuf.push(t);
  }
  flushLi(); flushPara();
  s = out.join('');
  /* 把代码块占位符换回真实内容 */
  s = s.replace(/<pre>\s*ZBLOCK(\d+)\s*<\/pre>/g, function(m, i){ return blocks[+i]; });
  s = s.replace(/ZBLOCK(\d+)Z/g, function(m, i){ return blocks[+i]; });
  return s;
}
/* ---------- 快捷问题：**可配置**（增删改，存本地） ----------
   每个人常用的问题不一样（HR 问的、主管问的、面试官问的都不一样），
   写死一组对谁都不合适 —— 让人自己配。 */
var CHAT_ASKS = null;
function chatAsksLoad(){
  if(CHAT_ASKS) return CHAT_ASKS;
  try{
    var raw = localStorage.getItem('chat_asks');
    CHAT_ASKS = raw ? JSON.parse(raw) :
      ['人才库里现在有几个人？', '帮我看看有哪些人是 A 档', '有哪些人还缺联系方式？', '给我一份面试提纲'];
  }catch(e){
    CHAT_ASKS = ['人才库里现在有几个人？', '帮我看看有哪些人是 A 档',
                 '有哪些人还缺联系方式？', '给我一份面试提纲'];
  }
  if(!CHAT_ASKS.length) CHAT_ASKS = ['人才库里现在有几个人？'];
  return CHAT_ASKS;
}
function chatAsksSave(list){
  CHAT_ASKS = list;
  try{ localStorage.setItem('chat_asks', JSON.stringify(list)); }catch(e){}
  chatAsksRender();
}
function chatAsksRender(){
  var box = document.getElementById('chatAsks');
  if(!box) return;
  var list = chatAsksLoad();
  box.innerHTML = list.length ? list.map(function(q){
    return '<button class="ask" data-q="' + esc(q) + '">' + esc(q) + '</button>';
  }).join('') : '<div class="hint">还没有快捷问题，点「配置」加几个</div>';
  Array.prototype.forEach.call(box.querySelectorAll('.ask'), function(b){
    b.addEventListener('click', function(){
      var inp = document.getElementById('chatIn');
      if(inp){ inp.value = b.dataset.q; askAgent(); }
    });
  });
}
function chatAsksConfig(){
  var list = chatAsksLoad().slice();
  var rows = list.map(function(q, i){
    return '<div class="crow" style="margin:4px 0"><label>第 ' + (i+1) + ' 条</label>' +
      '<div class="cinput"><input class="ced" data-ak="' + i + '" value="' + esc(q) + '">' +
      '<button class="btn sm" onclick="chatAskDel(' + i + ')">删除</button></div></div>';
  }).join('');
  openProgDlg('配置快捷问题',
    rows +
    '<div class="crow" style="margin-top:8px"><label>新增</label>' +
      '<div class="cinput"><input class="ced" id="chatAskNew" placeholder="写一条常用问题">' +
      '<button class="btn sm" onclick="chatAskAdd()">添加</button></div></div>' +
    '<div class="hint" style="margin-top:8px">改完点「保存」生效；这些只存在你自己的浏览器里。</div>' +
    '<div class="bar" style="margin-top:16px;justify-content:flex-end">' +
      '<button class="btn primary" onclick="chatAsksSaveNow()">保存</button></div>');
}
function chatAsksSaveNow(){
  var list = [];
  Array.prototype.forEach.call(document.querySelectorAll('#edDlgBody [data-ak]'), function(inp){
    var v = (inp.value || '').trim();
    if(v) list.push(v);
  });
  chatAsksSave(list);
  closeProgDlg();
  toast('已保存 ' + list.length + ' 条快捷问题');
}
function chatAskDel(i){
  var list = chatAsksLoad().slice();
  list.splice(i, 1);
  chatAsksSave(list);
  chatAsksConfig();
}
function chatAskAdd(){
  var inp = document.getElementById('chatAskNew');
  if(!inp) return;
  var v = (inp.value || '').trim();
  if(!v){ toast('先写内容'); return; }
  var list = chatAsksLoad().slice();
  list.push(v);
  chatAsksSave(list);
  chatAsksConfig();
}

async function initChat(){
  var box = document.getElementById('chatView');
  if(!document.getElementById('chatLog')){
    box.innerHTML =
      '<div class="chatwrap">' +
        '<div class="chatlog" id="chatLog"></div>' +
        '<div class="chatinput"><div class="ciwrap">' +
          '<textarea id="chatIn" rows="3" placeholder="输入指令或问题"></textarea>' +
          '<button class="btn primary" id="chatSend">发送</button>' +
          '<button class="btn stop" id="chatStop" style="display:none">停止</button>' +
        '</div></div>' +
      '</div>' +
      '<div class="chatside">' +
        '<div class="sec" style="margin-top:0"><h3 class="askhead">可以这样问' +
          '<button class="btn sm" onclick="chatAsksConfig()">配置</button></h3>' +
          '<div class="asks" id="chatAsks"></div></div>' +
        '<div class="sec"><h3>工具清单</h3><div class="hint" id="chatTools">读取中…</div></div>' +
        '<div class="sec"><h3>最近运行</h3><div id="chatRuns"></div></div>' +
        '<div class="sec"><button class="btn" id="chatClear">清空对话</button> ' +
          '<button class="btn" id="chatRestore">恢复对话</button></div>' +
      '</div>';
    var inp = document.getElementById('chatIn');
    inp.addEventListener('keydown', function(ev){
      if(ev.key === 'Enter' && !ev.shiftKey){ ev.preventDefault(); askAgent(); }
    });
    inp.addEventListener('input', function(){ chatGrow(inp); });
    setTimeout(function(){ chatGrow(inp); }, 0);
    document.getElementById('chatSend').addEventListener('click', askAgent);
    document.getElementById('chatStop').addEventListener('click', function(){
      if(CHAT_CTRL){ CHAT_CTRL.abort(); toast('已停止'); }
    });
    document.getElementById('chatRestore').addEventListener('click', restoreChat);
    document.getElementById('chatClear').addEventListener('click', async function(){
      var ok = await askConfirm({title: '清空对话', danger: true, okText: '清空',
        body: '清空与智能助手的全部对话记录？'});
      if(!ok) return;
      await api('/api/agent/clear', {method:'POST'});
      CHAT = [];
      document.getElementById('chatLog').innerHTML = '';
      toast('已清空');
    });
    chatAsksRender();
  }
  /* ⚠️ `/api/agent/history` 返回的是**对象** `{messages, restorable, purge_after…}`
     （`db.py:3836chat_history`），不是数组。原来写的是 `if(h && h.length)`
     —— 数组才有 length，对象恒为 undefined → **历史永远回显不出来**，
     刷新页面就只剩空白（而这正是这个接口存在的理由："没点清空就不该丢"）。
     另外要带上卡片：v1.25 起每轮还会带 draft/proposal 卡。 */
  var h = await api('/api/agent/history');
  var hmsgs = (h && h.messages) || [];
  var chatLog = document.getElementById('chatLog');
  if(chatLog) chatLog.innerHTML = '';
  if(hmsgs.length){
    hmsgs.forEach(function(m){
      var node = pushMsg(m.role, m.content);
      if(m.role === 'assistant'){
        var bits = [];
        if(m.mode) bits.push('模式：' + m.mode);
        if(m.rounds) bits.push('轮数 ' + m.rounds);
        if(m.latency_ms) bits.push(m.latency_ms + ' ms');
        if((m.tools || []).length) bits.push('工具：' + m.tools.map(function(x){ return x.tool; }).join(' → '));
        if(bits.length){
          node.insertAdjacentHTML('beforeend',
            '<details class="tracebox"><summary>本次调用详情</summary>' +
            '<div class="trace">' + esc(bits.join(' · ')) + '</div></details>');
        }
      }
      if((m.drafts || []).length) renderDraftCards(node, m.drafts);
      if((m.proposals || []).length) renderProposalCards(node, m.proposals);
    });
  }
  CHAT = hmsgs.map(function(x){ return {role: x.role, content: x.content}; });
  if(h && h.restorable === false && hmsgs.length){
    toast('历史对话已超过保留期，这一段可能看不全', 'warn');
  }
  var t = await api('/api/agent/tools');
  var el = document.getElementById('chatTools');
  if(t && t.total){
    el.innerHTML = '<div class="hint">共 ' + t.total + ' 个工具：读 ' +
      ((t.read || []).length) + ' · 算 ' + ((t.compute || []).length) + ' · 写 ' +
      ((t.write_requires_confirmation || []).length) + '（写操作一律要你确认）</div>';
  }
  var runs = await api('/api/agent/runs');
  var rb = document.getElementById('chatRuns');
  var runItems = (runs && runs.runs) || [];
  if(runItems.length){
    rb.innerHTML = runItems.slice(0, 8).map(function(r){
      return '<div class="doc"><span class="doc-n">' + esc(String(r.started_at || '').slice(5, 16)) +
        '　' + esc(r.question || r.prompt || '').slice(0, 24) + '</span>' +
        '<span class="doc-a"><span class="chipl gray">' + esc(r.mode || '') + '</span></span></div>';
    }).join('');
  }else{ rb.innerHTML = '<div class="hint">还没有运行记录</div>'; }
  killAutofill();
}
/* 输入框随内容长高：先压回 auto 量出真实高度，再夹到 10 行上限；
   超过上限就改成可滚动（内容从底部往上滚，而不是把框继续撑高）。 */
function chatGrow(inp){
  if(!inp) return;
  inp.style.height = 'auto';
  var lh = parseFloat(getComputedStyle(inp).lineHeight);
  if(!lh || isNaN(lh)) lh = 24;
  var max = Math.round(lh * 10);
  var need = inp.scrollHeight;
  inp.style.height = Math.min(need, max) + 'px';
  inp.style.overflowY = (need > max) ? 'auto' : 'hidden';
  if(need > max) inp.scrollTop = inp.scrollHeight;
}
function pushMsg(role, content){
  var log = document.getElementById('chatLog');
  var el = document.createElement('div');
  el.className = 'msg ' + role;
  el.innerHTML = role === 'assistant' ? mdLite(content) : esc(content);
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}
/* 模型一次调用可能要几十秒 —— 必须能**中断**，否则 HR 只能干等或刷新页面。
   用 AbortController 挂在 fetch 上：点「停止」直接 abort，界面立刻回到可输入状态。 */
var CHAT_CTRL = null;
function chatBusy(on){
  var send = document.getElementById('chatSend');
  var stop = document.getElementById('chatStop');
  var ta = document.getElementById('chatIn');
  if(send){ send.classList.toggle('busy', !!on); send.textContent = on ? '发送中…' : '发送'; }
  if(stop) stop.style.display = on ? '' : 'none';
  /* 忙碌态凭空多一个「停止」按钮，按钮组整体变宽 —— 输入框右侧的留白必须跟着加，
     否则长句子的末尾会被「发送中…」压住（v2 把按钮改成了框内绝对定位，
     原版按钮在框外正常流，没有这个问题）。
     宽度按实际按钮量出来，文案再变也不会对不上。 */
  if(ta){
    ta.style.paddingRight = '';
    var base = parseFloat(getComputedStyle(ta).paddingRight) || 92;
    var need = on ? Math.ceil(send.offsetWidth + 12 + stop.offsetWidth + 8 + 8) : base;
    ta.style.paddingRight = need + 'px';
  }
}
async function askAgent(){
  var inp = document.getElementById('chatIn');
  var q = (inp.value || '').trim();
  if(!q || CHAT_CTRL) return;
  inp.value = '';
  chatGrow(inp);
  pushMsg('user', q);
  var holder = pushMsg('assistant', '思考中…');
  holder.innerHTML = '<span class="hint">思考中…</span>';
  CHAT_CTRL = new AbortController();
  chatBusy(true);
  var r = await api('/api/agent/chat', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({message: q, history: CHAT}),
    signal: CHAT_CTRL.signal});
  CHAT_CTRL = null;
  chatBusy(false);
  if(r.__err && /abort/i.test(r.__err)){
    holder.innerHTML = '<span class="hint">已停止。</span>';
    return;
  }
  var answer = r.answer || (r.detail || '（无回复）');
  holder.innerHTML = mdLite(answer);
  if(r.trace && r.trace.length){
    /* 工具轨迹**默认折叠** —— HR 不关心函数名和 JSON 参数。
       但有 [已拦截] 时**默认展开**：那是"这件事没做"的原因，不能藏起来。 */
    var blocked = r.trace.some(function(t){ return t.disabled; });
    var lines = r.trace.map(function(t){
      return '→ ' + esc(t.tool_label || t.tool) + '（' + esc(JSON.stringify(t.args)) + '）' +
        (t.write ? ' <b>[写·需确认]</b>' : '') +
        (t.disabled ? ' <b>[已拦截]</b>' : '') +
        (t.recovered ? ' <b>[已补做]</b>' : '');
    }).join('<br>');
    holder.insertAdjacentHTML('beforeend',
      '<details class="tracebox"' + (blocked ? ' open' : '') + '>' +
      '<summary>工具调用（' + r.trace.length + ' 次' + (blocked ? ' · 有被拦截的' : '') + '）</summary>' +
      '<div class="trace">' + lines + '</div></details>');
  }
  if(r.mode){
    holder.insertAdjacentHTML('beforeend',
      '<div class="trace">模式：' + esc(r.mode) + ' · 轮数 ' + (r.rounds || 0) +
      ' · ' + (r.latency_ms || 0) + ' ms</div>');
  }
  CHAT.push({role:'user', content:q}, {role:'assistant', content: r.answer || ''});
  if((r.drafts || []).length) renderDraftCards(holder, r.drafts);
  if((r.pending_proposals || []).length) renderProposalCards(holder, r.pending_proposals);
  killAutofill();
}
function renderDraftCards(holder, drafts){
  drafts.forEach(function(d){
    var el = document.createElement('div');
    el.className = 'dcard';
    el.innerHTML = '<div class="dcard-t">' + esc(d.subject || '（邮件草稿）') + '</div>' +
      '<div class="dcard-b">' + esc(String(d.body || d.text || '').replace(/<[^>]*>/g, '').slice(0, 160)) + '</div>' +
      '<div class="dcard-a">' +
        '<button class="btn" data-draft="' + esc(d.id || '') + '">在写邮件中打开</button>' +
        /* 复制（业务方 2026-10-21：原版有、v2 缺）。
           HR 常常只是想把这段文字粘到微信/企业微信里，不一定真要走发信流程，
           逼他"先打开写邮件页再复制"多绕两步。 */
        '<button class="btn" data-copydraft="' + esc(d.id || '') + '">复制主题与正文</button>' +
      '</div>';
    holder.appendChild(el);
    var cb = el.querySelector('button[data-copydraft]');
    if(cb) cb.addEventListener('click', function(){
      var raw = window.__DRAFTS || {};
      var dd = raw[d.id] || d;
      var body = String(dd.body || dd.html || dd.text || '').replace(/<[^>]*>/g, '');
      copyText((dd.subject ? ('主题：' + dd.subject + '\n\n') : '') + body);
    });
    var b = el.querySelector('button[data-draft]');
    if(b) b.addEventListener('click', function(){
      var raw = window.__DRAFTS || {};
      var dd = raw[d.id] || d;
      setView('mail');
      setTimeout(function(){
        var s = document.getElementById('mSubject');
        var bd = document.getElementById('mBody');
        if(s) s.value = dd.subject || '';
        if(bd) bd.innerHTML = dd.body || dd.html || '';
        if(dd.to) { var t = document.getElementById('mToManual'); if(t) t.value = dd.to; }
        toast('已填入写邮件页');
      }, 400);
    });
  });
}
function renderProposalCards(holder, props){
  var el = document.createElement('div');
  el.className = 'dcard prop';
  /* 每条提案**直接在卡片上确认/拒绝**（业务方 2026-10-21：原版有、v2 缺）。
     原来这里只有一句"需要你在对应页面确认" —— 等于把 HR 从助手页赶去人才库，
     而助手恰恰是他刚提问的地方。写操作仍需人点头，只是把这个"点头"放到手边。 */
  el.innerHTML = '<div class="dcard-t">待确认提案（' + props.length + '）</div>' +
    props.map(function(p){
      return '<div class="proprow">' +
        '<div class="proprow-t">' + esc(p.title || p.summary || p.description || '') +
          (p.risk ? '<span class="prisk r' + esc(p.risk) + '">' + esc(p.risk) + '风险</span>' : '') +
        '</div>' +
        '<div class="proprow-a">' +
          '<button class="btn primary sm" data-prop="' + p.id + '" data-d="approve">同意执行</button>' +
          '<button class="btn sm" data-prop="' + p.id + '" data-d="reject">拒绝</button>' +
        '</div></div>';
    }).join('') +
    '<div class="dcard-a"><span class="hint">同意后立即执行并写审计；拒绝也留痕。' +
      '写操作不会自动生效。</span></div>';
  holder.appendChild(el);
  Array.prototype.forEach.call(el.querySelectorAll('[data-prop]'), function(b){
    b.addEventListener('click', function(){
      b.disabled = true;
      decideProp(Number(b.dataset.prop), b.dataset.d, b);
    });
  });
}

/* ---------- 面试单位：多选（照搬原版 multiRender/multiAdd）----------
   原版语义：**一个候选人可能由多个单位面试**（如「分析测试中心、稀土研究院」），
   存库用**顿号**连接，筛选按边界匹配。所以这里不能是一个普通文本框 ——
   要做成 chips + 可下拉输入，回车/失焦把输入按分隔符拆成多个。 */
var MULTI_IVUNIT = [];
function multiRenderIvUnit(){
  var box = document.getElementById('multi_ivunit');
  if(!box) return;
  box.innerHTML = MULTI_IVUNIT.map(function(v, i){
    return '<span class="mchip">' + esc(v) +
      '<b onclick="multiDelIvUnit(' + i + ')" title="移除">×</b></span>';
  }).join('') +
    '<input id="madd_ivunit" list="dlIvUnit" placeholder="选面试单位（可多选）或直接输入">' +
    '<datalist id="dlIvUnit">' + (IDICT.units || []).map(function(x){
      return '<option value="' + esc(x) + '"></option>'; }).join('') + '</datalist>';
  var inp = document.getElementById('madd_ivunit');
  if(inp){
    inp.addEventListener('keydown', function(ev){
      if(ev.key === 'Enter'){ ev.preventDefault(); multiAddIvUnit(); }
    });
    inp.addEventListener('change', multiAddIvUnit);
  }
  var hid = document.getElementById('ivUnit');
  if(hid) hid.value = MULTI_IVUNIT.join('、');
  killAutofill();
}
function multiAddIvUnit(){
  var inp = document.getElementById('madd_ivunit');
  if(!inp) return;
  var raw = (inp.value || '').trim();
  if(!raw){ inp.focus(); return; }
  /* 粘贴「A、B、C」时**按分隔符拆成多个**（原版 v1.29.11 的口径）：
     只删分隔符会把一串糊成一个词。 */
  var added = 0;
  raw.split(/[、，,;；\n]+/).forEach(function(s){
    s = s.trim();
    if(!s || MULTI_IVUNIT.indexOf(s) >= 0) return;
    MULTI_IVUNIT.push(s); added++;
  });
  if(!added) toast('没有新增（都已在列表里）');
  inp.value = '';
  multiRenderIvUnit();
  var again = document.getElementById('madd_ivunit');
  if(again) again.focus();
}
function multiDelIvUnit(i){
  MULTI_IVUNIT.splice(i, 1);
  multiRenderIvUnit();
}

/* ---------- 待确认提案（**原版的核心工作流之一**，v2 之前完全没有） ----------
   智能助手/自动扫描会生成"建议"，都要 HR 亲自点头才落库。
   同意 → 执行并写审计；拒绝 → 也写审计（谁在什么时候拒的）。
   放在人才库第二行，点开是一张可折叠的清单。 */
var PROPS = [];
function toggleProps(){
  var box = document.getElementById('propBox');
  var open = box.classList.toggle('on');
  document.getElementById('btnProp').classList.toggle('on', open);
  if(open) loadProps();
}
async function loadProps(){
  var box = document.getElementById('propBox');
  box.innerHTML = '<div class="hint" style="padding:8px">读取提案…</div>';
  var r = await api('/api/proposals');
  PROPS = (r.items || []).filter(function(x){ return x.status === '待确认'; });
  document.getElementById('btnProp').textContent = '待确认提案（' + PROPS.length + '）';
  if(!PROPS.length){ box.innerHTML = '<div class="hint" style="padding:10px">没有待确认的提案</div>'; return; }
  box.innerHTML = (PROPS.length ? '<div class="prow" style="border-bottom:1px solid var(--line)">' +
    '<span class="ptext hint">共 ' + PROPS.length + ' 条待确认</span>' +
    '<span class="pacts">' +
      '<button class="btn sm" onclick="decideAllProps(\'approve\')">全部确认执行</button>' +
      '<button class="btn sm" onclick="decideAllProps(\'reject\')">全部拒绝</button>' +
    '</span></div>' : '') + PROPS.map(function(p){
    return '<div class="prow" id="prop-' + p.id + '">' +
      '<div class="ptext">' + esc(p.summary || '') + '</div>' +
      '<span class="prisk r' + esc(p.risk || '低') + '">' + esc(p.risk || '低') + '风险</span>' +
      '<span class="pacts">' +
        '<button class="btn primary sm" onclick="decideProp(' + p.id + ',\'approve\',this)">同意</button>' +
        '<button class="btn sm" onclick="decideProp(' + p.id + ',\'reject\',this)">拒绝</button>' +
      '</span></div>';
  }).join('');
}
/* 批量决定：逐条走同一个 decide 接口（每条都写审计），失败不中断，最后汇总 */
async function decideAllProps(decision){
  var ids = PROPS.map(function(p){ return p.id; });
  if(!ids.length){ toast('没有待确认的提案'); return; }
  var ok = await askConfirm({title: decision === 'approve' ? '全部确认执行' : '全部拒绝',
    danger: decision === 'reject',
    body: '将对 ' + ids.length + ' 条提案' +
          (decision === 'approve' ? '逐条执行（会真的改档案，写审计）' : '逐条拒绝（也写审计）') + '。继续？'});
  if(!ok) return;
  var done = 0, fail = 0;
  for(var i = 0; i < ids.length; i++){
    var r = await api('/api/proposals/' + ids[i] + '/decide', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({decision: decision})});
    if(r.__err) fail++; else done++;
  }
  toast('已处理 ' + done + ' 条' + (fail ? ('，失败 ' + fail + ' 条') : ''));
  loadProps(); load();
}
async function decideProp(pid, decision, btn){
  var q = decision === 'approve'
    ? '确认执行这条提案吗？\n\n它会真的改档案（写审计）。'
    : '拒绝这条提案？\n\n拒绝也会写入审计。';
  var ok = await askConfirm({title: decision === 'approve' ? '同意提案' : '拒绝提案',
    body: q, danger: decision === 'reject',
    okText: decision === 'approve' ? '同意并执行' : '拒绝'});
  if(!ok) return;
  if(btn) btn.disabled = true;
  var r = await api('/api/proposals/' + pid + '/decide', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({decision: decision})});
  if(r.__err){ toast(r.__err); if(btn) btn.disabled = false; return; }
  var row = document.getElementById('prop-' + pid);
  if(row) row.classList.add('done');
  toast(decision === 'approve' ? '已执行' : '已拒绝');
  loadProps(); load();
}
/* ---------- 批量：按模型重跑 / 分析待分析的人 ---------- */
async function batchReanalyze(){
  var ok = await askConfirm({title: '批量按模型重跑',
    body: '对「抽取方式还是规则通道」的档案逐人完整重跑？\n\n每人约 10 秒，会调用模型。',
    okText: '开始重跑'});
  if(!ok) return;
  toast('开始重跑…');
  var total = 0;
  /* 后端返回的是 `analyzed`（`server.py:1717/1731`），原来这里读 `r.reanalyzed`
     → 恒为 0，进度与结果永远显示 0 人。
     每批也从 1 人改成 10 人（后端上限 12，`server.py:1719`），并把「循环 20 次」
     这个**硬编码业务上限**去掉 —— 超过 20 人时原来会中途静默结束、看起来像"跑完了"。
     留一个 300 的 guard 只防后端 remaining 不收敛的死循环。 */
  for(var guard = 0; guard < 300; guard++){
    var r = await api('/api/candidates/reanalyze-batch', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({limit: 10, only_heuristic: true})});
    if(r.__err){ toast(r.__err); return; }
    total += (r.analyzed || 0);
    if(!r.remaining) break;
    /* 本批一个都没成功 = **没有进展**（多半是这批人反复失败，例如模型不可用）。
       不停下来的话 remaining 永远不为 0，会空转到 guard 上限——几千次请求把界面卡死，
       而用户看到的只是进度条不动。宁可停下并说清楚原因。 */
    if(!(r.analyzed || 0)){
      toast('连续一批没有成功（失败 ' + (r.failed || 0) + ' 人），已停下；'
            + '请先确认模型可用，再点一次继续', 'warn');
      break;
    }
    toast('已重跑 ' + total + ' 人，还剩 ' + r.remaining + ' 人…');
  }
  toast('批量重跑完成，共 ' + total + ' 人');
  load();
}
async function analyzePending(){
  /* 作用：给「入库即分析」开关关着时漏掉的人**补做分析**（AI 抽技能、算档位那一整套）。
     ⚠️ 原来只调一次、limit 写死 5 —— 有 50 人没分析就得连点 10 次，
     按钮名是"分析待分析的人"，做一半就停等于名不副实。
     现在循环到没有剩余为止（每轮 10 人，最多 50 轮封顶防跑飞）。 */
  var total = 0, rounds = 0;
  toast('正在分析…');
  while(rounds < 50){
    var r = await api('/api/insights/analyze-pending', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({limit: 10})});
    if(r.__err){ toast(r.__err, 'danger'); break; }
    total += Number(r.analyzed) || 0;
    rounds++;
    /* 这一轮一个人都没分析出来 → 说明要么没有待分析的，要么已经处理完了 */
    if(!(Number(r.analyzed) > 0)) break;
  }
  if(total) toast('已分析 ' + total + ' 人', 'ok');
  else toast('没有待分析的人', 'warn');
  load();
}

/* ---------- 结论标签：取值范围**按阶段限定**（stages.json 的 outcomes）----------
   待面试/已面试 → 拟录用 · 不录用；已发放offer → 已签约 · 拒签；其它阶段不允许打结论。
   之前 v2 固定给「未判/拟录用/不录用」，在「已发放offer」阶段提交必然被服务端 422 ——
   业务方看到的就是"下拉能选、点了没反应"。 */
function outcomeField(d){
  var outs = (STAGE_CFG.outcomes || {})[(d.stage || '').trim()] || [];
  var cur = d.outcome || '';
  var badge = outcomeChip(cur, d.stage);
  if(!outs.length){
    /* 空状态要给出**下一步**，而不只是宣布"这里不能用"：
       阶段没到能下结论的时候，告诉 HR 到哪个阶段可以标。 */
    var can = [];
    Object.keys(STAGE_CFG.outcomes || {}).forEach(function(k){
      if((STAGE_CFG.outcomes[k] || []).length) can.push(k);
    });
    var tip = can.length ? ('可在「' + can.join('」「') + '」阶段标结论') : '当前流程没有可标结论的阶段';
    return '<span class="hint">' + (cur ? esc(cur) + '　' : '') + tip + '</span>';
  }
  /* 现值不属于本阶段允许的结论（历史数据）时，**照样列出来** ——
     否则下拉显示"未判"、列表显示"已签约"，同一件事两处说法。 */
  var opts = [''].concat(outs);
  if(cur && opts.indexOf(cur) < 0) opts.push(cur);
  var order = STAGE_CFG.order || [];
  var stageIdx = order.indexOf((d.stage || '').trim());
  var interviewIdx = order.indexOf('已面试');
  var autoAdvance = stageIdx >= 0 && interviewIdx >= 0 && stageIdx < interviewIdx;
  var title = autoAdvance
    ? '面试结论（选择后会从当前阶段自动推进到「已面试」）'
    : '面试结论（只改标签，不改变当前阶段）';
  /* 徽标 / 下拉 / 「清空结论」三点必须**同一行**：`结论` 这一格在 .ivrow 里是
     纵向 flex，直接平铺的话按钮会掉到下拉下面单独占一行（2026-10-28 修）。 */
  return '<span class="ivout">' + badge +
    '<select id="ivOutcome" class="vsel" title="' + esc(title) + '">' +
    opts.map(function(v){
      return '<option value="' + esc(v) + '"' + (cur === v ? ' selected' : '') + '>' +
        (v || '未判') + '</option>';
    }).join('') + '</select>' +
    /* 「清空结论」是**撤销一个已经做过的判断**，用危险动作的红（白底灰边红字，
       hover 才变红底）—— 与「覆盖一切条件」同族，不该和普通次要按钮长一样。 */
    '<button class="btn sm btn-danger" onclick="clearOutcome()">清空结论</button></span>';
}
/* ---------- 改结论标签：前序阶段会补推，后续阶段只改标签 ----------
   业务方 2026-10-20 16:0x：「我阶段没打错，我可能也会点错，标签应该可以修改」。

    2026-10-21 补充：有面试结论就意味着面试已经发生；如果人还停在
    「已面试」之前，不能只落标签。服务端会把「当前阶段 + 新结论」
    统一解析为「已面试 + 新结论」；已在「已面试」或之后则保持阶段不动。
    走 stage 接口（而不是 interview-info）还有一个好处：
   interview-info 的值域写死为「已面试」那一档（拟录用/不录用），
   在「已发放offer」阶段根本改不了 已签约/拒签。

   ⚠️ 这里不会把「不录用」推进到「已结束」或触发归档；结束流程仍由 HR 显式操作。 */
async function setOutcome(aid, stage, v){
  var body = {stage: stage || '', outcome: v || ''};
  var r = await api('/api/applications/' + aid + '/stage', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if(r.__err){ toast(r.__err); return; }
  var msg = v ? ('已把结论改为「' + v + '」') : '已清除结论（未判）';
  if(r.advanced_to) msg += '，并自动推进到「' + r.advanced_to + '」';
  toast(msg);
  load(); if(CUR) pick(CUR);
}
async function clearOutcome(){
  var aid = CUR ? (CUR_DETAIL || {}).application_id : null;
  if(!aid){ toast('这条投递还没有编号'); return; }
  var st = (CUR_DETAIL || {}).stage || '';
  var ok = await askConfirm({title: '清空结论', okText: '清空',
    body: '把结论标签清掉（回到「未判」）？\n\n只清标签，不会动阶段。'});
  if(!ok) return;
  setOutcome(aid, st, '');
}
/* ---------- 阶段推进 ----------
   阶段流转**只用档案头部那一个下拉**（#stageSel）。
   原版的核心交互是"推进阶段时顺带写结论"（已面试 → 拟录用 / 不录用 → 已结束）。
   v2 把这两件事拆开了：推进走下拉，结论走**按阶段限定值域**的结论下拉
   （见 `outcomeField`，值域来自 stages.json 的 outcomes）——功能都在，但不再
   为每个动作单配一个按钮（业务方 2026-10-21：不需要"保存更正"左边的那些推进按钮）。
   stages.json 的 actions 段保留为配置（唯一真源），将来若要恢复"一键推进+打标签"
   的按钮组，直接照它渲染即可，不必改数据。 */
/* 生成 JS 字符串字面量（带引号）—— 拼 onclick 参数时用这个，
   不要手写 '\\'' 那种转义：少一个引号就是整页 JS 语法错。 */
function jq(v){
  return "'" + String(v == null ? '' : v).replace(/'/g, "\\'") + "'";
}
async function doStageAction(aid, to, outcome){
  var body = {stage: to};
  if(outcome) body.outcome = outcome;
  var r = await api('/api/applications/' + aid + '/stage', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if(r.__err){ toast(r.__err); return; }
  toast(r.note || ('已推进到「' + to + '」'));
  load(); if(CUR) pick(CUR);
}
/* 一键复制基本信息（业务方 2026-10-21）。
   HR 常常要把候选人信息发到微信/短信/群里，逐项选中复制很烦。
   取值顺序是**先读输入框当前值**（包含刚改还没点「保存更正」的），
   读不到才退回库里下发的 d —— 否则复制出来的是改动之前的内容，容易发错。 */
function copyBasicInfo(){
  var d = CUR_DETAIL || {};
  function cur(id, fallback){
    var el = document.getElementById(id);
    return (el && el.value !== '' && el.value != null) ? el.value : (fallback || '');
  }
  var rows = [
    ['姓名', cur('edName', d.name)],
    ['学历', cur('edEdu', d.edu_level)],
    ['性别', cur('edGender', d.gender)],
    ['四六级', cur('edCetLv', d.cet_level)],
    ['电话', cur('edPhone', d.phone)],
    ['邮箱', cur('edEmail', d.email)],
    ['学校', cur('edSchool', d.school)],
    ['专业', cur('edMajor', d.major)]
  ].filter(function(r){ return r[1] && r[1] !== '—'; });
  if(!rows.length){ toast('这个人还没有可复制的信息', 'warn'); return; }
  copyText(rows.map(function(r){ return r[0] + '：' + r[1]; }).join('\n'));
}
/* 复制到剪贴板 */
function copyText(t){
  if(!t) return;
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(function(){ toast('已复制'); },
      function(){ toast('复制失败，请手动选中'); });
    return;
  }
  var ta = document.createElement('textarea');
  ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try{ document.execCommand('copy'); toast('已复制'); }
  catch(e){ toast('复制失败，请手动选中'); }
  ta.remove();
}

/* ---------- 档案里的额外动作（面试提纲 / 建议岗位 / 指定岗位 / 证件照） ---------- */
/* 面试提纲的结构化渲染（业务方 2026-10-21：原来直接把 JSON 原文铺在页面上）。
   后端 `draft_interview` 返回 `{"questions":[{"q":"问题","why":"考察意图"}]}`
   （见 `analyze.py:70` 的提示词约定），这里按序号 + 考察点排版；
   模型偶尔不按 JSON 返回时，再退回 Markdown 渲染。 */
function renderOutline(box, r){
  var qs = [];
  if(Array.isArray(r.questions)) qs = r.questions;
  else if(r.outline){
    var o = r.outline;
    if(typeof o === 'string'){ try{ o = JSON.parse(o); }catch(e){ o = null; } }
    if(o && Array.isArray(o.questions)) qs = o.questions;
  }
  qs = qs.filter(function(x){ return x && (x.q || x.question || x.text); });
  if(!qs.length) return false;
  box.innerHTML = '<div class="ivqwrap">' +
    '<div class="ivqh">面试提纲' + (r.job && r.job.title ? '（' + esc(r.job.title) + '）' : '') +
      '　共 ' + qs.length + ' 题</div>' +
    '<ol class="ivq">' + qs.map(function(x){
      var q = x.q || x.question || x.text || '';
      var why = x.why || x.focus || x.purpose || '';
      return '<li><div class="ivq-q">' + esc(q) + '</div>' +
        (why ? '<div class="ivq-w">考察：' + esc(why) + '</div>' : '') + '</li>';
    }).join('') + '</ol></div>';
  return true;
}
async function getOutline(cid){
  var box = document.getElementById('mOutline');
  if(!box) return;
  box.innerHTML = '<span class="hint">正在生成面试提纲…</span>';
  var r = await api('/api/candidates/' + cid + '/interview', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({focus: ''})});
  if(r.__err){ box.innerHTML = '<span class="hint">' + esc(r.__err) + '</span>'; return; }
  /* 未归岗等业务错误：说清下一步，别把 {"error": ...} 当正文铺出来 */
  if(r.error){
    box.innerHTML = '<div class="hint">' + esc(r.error) +
      (r.hint ? '<br>' + esc(r.hint) : '') + '</div>';
    return;
  }
  if(renderOutline(box, r)) return;
  /* 兜底：模型没按 JSON 返回（纯文本/带 Markdown）时照旧渲染 */
  var t = r.outline || r.text || r.content || r.answer || '';
  if(t && typeof t === 'object') t = '';
  box.innerHTML = t ? mdLite(String(t))
    : '<div class="hint">模型没有返回可用的提纲，可再点一次「面试提纲」重试。</div>';
}
async function adoptSuggestedJob(cid){
  /* ⚠️ 原来调的是 `/suggest-job`：那个接口**只把 AI 的建议写进 suggested_job_id**
     （`server.py:1647` → `regrade.py:333`），并不真的把人归到岗位上，
     候选人仍然是「未归岗」—— 而界面提示「已采纳建议岗位」，属于**假成功**。
     真正归岗是 `/assign-job`（原版走的就是它，`ui.py:2065`）。
     现在：先取建议 → 没建议就直说 → 有建议则真正归岗，并报出岗位名。 */
  var r = await api('/api/candidates/' + cid + '/suggest-job', {method:'POST'});
  if(r.__err){ toast(r.__err, 'danger'); return; }
  /* 字段以服务端为准：`/suggest-job` 返回 `job_id` / `title` / `reason`
     （`regrade.py:379`），`/assign-job` 返回的才是 `job_title`（`regrade.py:464`）。 */
  var jid = r.job_id || 0;
  var title = r.title || r.job_title || '';
  if(!jid){
    toast(r.note || r.error || '模型没能给出岗位建议，可点「指定岗位」手动选', 'warn');
    return;
  }
  var okA = await askConfirm({title: '采纳建议岗位',
    body: '把这个人归到「' + (title || ('岗位 #' + jid)) + '」？'
        + '\n\n归岗后会按该岗位的 JD 参与档位判定与后续流程。', okText: '归岗'});
  if(!okA) return;
  var a = await api('/api/candidates/' + cid + '/assign-job', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({job_id: Number(jid)})});
  if(a.__err){ toast(a.__err, 'danger'); return; }
  toast(a.job_title ? ('已归岗到「' + a.job_title + '」') : '已归岗', 'ok');
  load(); if(CUR) pick(CUR);
}
async function openAssignJob(cid){
  var j = await api('/api/jobs');
  /* v4.0.4 修：原来用 askText 让 HR **手打岗位全名**，而「岗位管理」里建出来的岗位
     一个都没列出来 —— 看上去就是"不弹我建的岗位"。改成把在招岗位摆成下拉。
     停用的岗位不列（归岗只对在招岗位有意义），但空列表时的提示要说清去哪儿建。 */
  var items = (j.items || []).filter(function(x){ return x.active; });
  if(!items.length){
    toast('还没有在招岗位，请先在「岗位管理」里新建', 'warn');
    return;
  }
  var pick = await askSelect('指定岗位', items.map(function(x){
    return {value: String(x.id),
            label: x.title + (x.applications_count ? '（在池 ' + x.applications_count + ' 人）' : '')};
  }), '选择一个在招岗位');
  if(!pick) return;
  var hit = items.filter(function(x){ return String(x.id) === String(pick); })[0];
  if(!hit) return;
  var r = await api('/api/candidates/' + cid + '/assign-job', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({job_id: hit.id})});
  if(r.__err){ toast(r.__err); return; }
  toast('已归岗到「' + hit.title + '」');
  load(); if(CUR) pick(CUR);
}
async function avatarRefresh(cid){
  var r = await api('/api/candidates/' + cid + '/avatar', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: '{}'});
  toast(r.__err ? ('失败：' + r.__err) : '已重新抽取证件照');
  if(CUR) pick(CUR);
}
async function avatarClear(cid){
  var ok = await askConfirm({title: '去掉证件照', body: '去掉这个人的证件照？'});
  if(!ok) return;
  var r = await api('/api/candidates/' + cid + '/avatar', {method:'DELETE'});
  toast(r.__err ? ('失败：' + r.__err) : '已去掉');
  if(CUR) pick(CUR);
}

/* ---------- 来源文件 / 运行设置 / 收信预览 / 主动扫描 / 恢复对话 ---------- */
async function loadSources(){
  var r = await api('/api/sources');
  var f = r.folder || {}, rec = r.recycle || {};
  var setp = function(id, txt){ var e=document.getElementById(id); if(e) e.textContent = txt; };
  setp('srcPath', (f.path || '') + '（' + (f.exists ? '存在' : '不存在') + '，' +
        (f.count || 0) + ' 个文件）');
  var files = (f.files || []).map(function(x){ return x.name || x; });
  setp('srcFiles', files.length ? files.length + ' 个' : '（没有待导入文件）');
  var box = document.getElementById('srcFileList');
  if(box){
    box.innerHTML = files.length ? files.map(function(fn){
      return '<div class="doc">' +
        '<label class="ck2"><input type="checkbox" class="srcchk" data-name="' + esc(fn) + '"></label>' +
        '<span class="doc-n">' + esc(fn) + '</span>' +
        '<span class="doc-a">' +
        '<button class="btn sm" onclick="openSourceFile(' + jq(fn) + ',1)">打开</button>' +
        '<button class="btn sm" onclick="openSourceFile(' + jq(fn) + ',0)">下载</button>' +
        '<button class="btn sm" onclick="srcRemoveOne(' + jq(fn) + ')">移除</button>' +
        '</span></div>';
    }).join('') : '<div class="hint">目录是空的</div>';
    Array.prototype.forEach.call(box.querySelectorAll('.srcchk'), function(c){
      c.addEventListener('change', srcChkInfo);
    });
  }
  var all = document.getElementById('srcChkAll');
  if(all){
    all.checked = false;
    all.onclick = function(){
      Array.prototype.forEach.call(document.querySelectorAll('.srcchk'), function(c){
        c.checked = all.checked;
      });
      srcChkInfo();
    };
  }
  srcChkInfo();
  setp('srcRecycle', '已移除 ' + (rec.count || 0) + ' 个');
  /* 回收站逐文件列出 + 逐个恢复（原来只有一句计数，只能"全部恢复"） */
  var recBox = document.getElementById('srcRecList');
  if(recBox){
    api('/api/sources/removed').then(function(rr){
      var items = (rr.items || []).map(function(x){ return x.name || x; });
      var el = document.getElementById('srcRecList');
      if(!el) return;
      el.innerHTML = items.length ? items.map(function(nm){
        return '<div class="doc"><span class="doc-n">' + esc(nm) + '</span>' +
          '<span class="doc-a">' +
          '<button class="btn sm" onclick="srcRestoreOne(' + jq(nm) + ')">恢复</button>' +
          '</span></div>';
      }).join('') : '';
    }).catch(function(){});
  }
}
function srcChkInfo(){
  var n = document.querySelectorAll('.srcchk:checked').length;
  var info = document.getElementById('srcChkInfo');
  if(info) info.textContent = n ? ('已选 ' + n + ' 个') : '';
}
async function srcRemoveOne(name){
  var ok = await askConfirm({title: '移入回收站', danger: true, okText: '移除',
    body: '把「' + name + '」移到回收站？\\n\\n文件不会删除，可在下面回收站里逐个恢复。'});
  if(!ok) return;
  var r = await api('/api/sources/remove', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({names: [name]})});
  toast(r.__err ? ('失败：' + r.__err) : '已移入回收站');
  loadSources();
}
async function srcRemoveChecked(){
  var names = Array.prototype.map.call(
    document.querySelectorAll('.srcchk:checked'), function(c){ return c.dataset.name; });
  if(!names.length){ toast('先勾选要移除的文件', 'warn'); return; }
  var ok = await askConfirm({title: '移入回收站', danger: true, okText: '移除 ' + names.length + ' 个',
    body: '把勾选的 ' + names.length + ' 个文件移到回收站？\\n\\n不会删除，可逐个恢复。'});
  if(!ok) return;
  var r = await api('/api/sources/remove', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({names: names})});
  toast(r.__err ? ('失败：' + r.__err) : ('已移除 ' + names.length + ' 个'));
  loadSources();
}
async function srcRestoreOne(name){
  var r = await api('/api/sources/restore', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({names: [name]})});
  toast(r.__err ? ('失败：' + r.__err) : ('已恢复「' + name + '」'));
  loadSources();
}
function openSourceFile(name, inline){
  var url = withToken('/api/sources/file?inline=' + (inline ? 1 : 0) +
    '&name=' + encodeURIComponent(name));
  if(inline) window.open(url, '_blank');
  else{ var a=document.createElement('a'); a.href=url; a.download='';
        document.body.appendChild(a); a.click(); a.remove(); }
}
async function srcAct(act){
  if(act === 'bundle'){
    /* 原来这里发的是 `{names: []}` —— 一个空数组，等于什么都没选，
       后端如实回「没有选择任何文件」，而界面还提示"简历包已生成"。
       现在真把来源目录里的待导入文件打出来（后端单次上限 200，`server.py:2987`）。 */
    var r = await api('/api/sources');
    if(r.__err){ toast(r.__err, 'danger'); return; }
    var files = ((r.folder || {}).files || []).map(function(x){ return x.name || x; })
      .filter(Boolean);
    if(!files.length){ toast('来源目录里没有待导入的文件', 'warn'); return; }
    var use = files.slice(0, 200);
    var okB = await askConfirm({title: '打包来源文件',
      body: '把来源目录里的 ' + use.length + ' 个文件打成一个 zip？'
        + (files.length > 200 ? '\n\n（单次最多 200 个，目录里共 ' + files.length
          + ' 个，本次只打包前 200 个）' : '')});
    if(!okB) return;
    toast('正在打包 ' + use.length + ' 个文件…');
    var rr = await fetch(withToken('/api/sources/bundle'),
      {method:'POST', headers: hdr(), body: JSON.stringify({names: use})});
    if(!rr.ok){
      var msg = '打包失败（HTTP ' + rr.status + '）';
      try{ var jj = await rr.json(); msg = jj.detail || msg; }catch(e){}
      toast(msg, 'danger'); return;
    }
    var skipped = parseInt(rr.headers.get('X-Skipped') || '0');
    saveBlob(await rr.blob(), '待导入简历打包.zip');
    toast('已导出 ' + use.length + ' 个文件'
          + (skipped ? ('；' + skipped + ' 个取不到，已跳过') : ''),
          skipped ? 'warn' : 'ok');
    return;
  }
  if(act === 'recycle'){
    var rr = await api('/api/sources/removed');
    var items = rr.items || [];
    if(!items.length){ toast('回收站是空的'); return; }
    var names = items.map(function(x){ return x.name || x; });
    var ok = await askConfirm({title: '回收站', okText: '全部恢复',
      body: '回收站里有 ' + names.length + ' 个文件：\n' +
            names.slice(0, 12).join('、') + '\n\n全部恢复回待导入目录？'});
    if(!ok) return;
    var r2 = await api('/api/sources/restore', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({names: names})});
    toast(r2.__err ? ('失败：' + r2.__err) : '已恢复 ' + (r2.restored || names.length) + ' 个文件');
    loadSources();
    return;
  }
  if(act === 'remove'){
    var s2 = await api('/api/sources');
    var files = (s2.folder || {}).files || [];
    if(!files.length){ toast('没有待移除的文件'); return; }
    var ok2 = await askConfirm({title: '移除来源文件', danger: true, okText: '移除',
      body: '把待导入目录里的 ' + files.length + ' 个文件移进回收站？\n（可随时恢复）'});
    if(!ok2) return;
    var r3 = await api('/api/sources/remove', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({names: files.map(function(x){ return x.name || x; })})});
    toast(r3.__err ? ('失败：' + r3.__err) : '已移入回收站');
    loadSources();
  }
}
async function loadSettings(){
  var r = await api('/api/settings');
  var setp = function(id, txt){ var e=document.getElementById(id); if(e) e.textContent = txt; };
  setp('setOverdue', (r.pipeline_overdue_days || 15) + ' 天');
  setp('setAuto', r.auto_insight_on_ingest ? '开' : '关');
  /* 现值填进编辑控件（点「修改」露出来时就是当前值） */
  var od = document.getElementById('cedv_pipeline_overdue_days');
  if(od) od.value = r.pipeline_overdue_days || 15;
  var ai = document.getElementById('cedv_auto_insight_on_ingest');
  if(ai) ai.checked = !!r.auto_insight_on_ingest;
}
async function saveSettings(){
  var od = document.getElementById('cedv_pipeline_overdue_days');
  var ai = document.getElementById('cedv_auto_insight_on_ingest');
  if(!od) return;
  var r = await api('/api/settings', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({pipeline_overdue_days: Number(od.value) || 15,
                          auto_insight_on_ingest: !!(ai && ai.checked)})});
  toast(r.__err ? ('失败：' + r.__err) : '设置已保存');
  loadSettings();
}
/* 收信预览：**在按钮下方弹出一个框**展示详情（原版做法：就地展开，不跳走）。
   顶部 toast 会被别的提示顶掉，弹窗又太重 —— 就地展开最省事。 */
async function previewMailbox(){
  var msg = document.getElementById('mbPvMsg');
  var box = document.getElementById('mbPvBox');
  if(msg){ msg.style.display = ''; msg.className = 'msgline busy'; msg.textContent = '正在读取收信…'; }
  if(box) box.style.display = 'none';
  var r = await api('/api/mailbox/preview', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({limit: 5})});
  if(r.__err){
    if(msg){ msg.className = 'msgline bad'; msg.textContent = '✗ ' + r.__err; }
    return;
  }
  var items = r.items || r.mails || [];
  if(!items.length){
    if(msg){ msg.className = 'msgline'; msg.textContent = '收件箱里没有可预览的邮件'; }
    return;
  }
  /* v3.1.2：把"邮箱共多少封"与"这次列了几封"分开写清楚。
     原来只显示"读到 N 封"（N = 预览条数，默认 5），HR 会以为邮箱里只有 5 封；
     而不同邮箱的"总数"看起来忽多忽少，其实一个是文件夹全部邮件数、
     一个是带简历附件的数 —— 两个数含义完全不同，必须各自标名。 */
  if(msg){
    msg.className = 'msgline';
    var parts = [];
    if(r.total != null) parts.push('邮箱共 ' + r.total + ' 封');
    if(r.resume_in_sample != null && r.sample_size)
      parts.push('最近 ' + r.sample_size + ' 封里 ' + r.resume_in_sample + ' 封带简历');
    msg.textContent = parts.length ? ('✓ ' + parts.join('，')) : ('读到 ' + items.length + ' 封邮件');
  }
  if(!box) return;
  box.style.display = 'block';
  box.innerHTML = '<div class="pvbox">' +
    '<div class="pvhead">' +
      (r.total != null
        ? '收件箱共 ' + r.total + ' 封，下面是最近 ' + items.length + ' 封'
        : '收件箱最新 ' + items.length + ' 封') +
    '</div>' +
    items.map(function(x){
      var frm = x.from || x.sender || '';
      var dt = String(x.date || x.received_at || '').slice(5, 16);
      var att = (x.has_attachment || x.attachments) ? ' 📎' : '';
      return '<div class="pvrow"><span class="pvsub" title="' + esc(x.subject || '') + '">' +
        esc(x.subject || '(无主题)') + '</span>' +
        '<span class="pvmeta">' + esc(frm) + (dt ? ' · ' + esc(dt) : '') + att + '</span></div>';
    }).join('') + '</div>';
}
async function runScan(){
  toast('正在扫描…');
  var r = await api('/api/proactive/scan', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({stuck_days: 7})});
  if(r.__err){ toast('失败：' + r.__err); return; }
  toast('扫描完成：新增 ' + (r.proposed || r.created || 0) + ' 条待确认提案');
  loadProps();
}
async function restoreChat(){
  var ok = await askConfirm({title: '恢复对话', okText: '恢复',
    body: '恢复被清空前的对话记录？'});
  if(!ok) return;
  var r = await api('/api/agent/restore', {method:'POST'});
  toast(r.__err ? ('失败：' + r.__err) : '已恢复');
  initChat();
}

/* ---------- 输入框一律不弹浏览器的历史记录 / 自动填充建议 ----------
   业务方 2026-10-20 明确要求。浏览器会把以前填过的值存下来，点输入框就弹下拉，
   在"姓名/邮箱"这种字段上尤其烦（还可能把上一个人的邮箱带出来）。
   用 MutationObserver 兜住**所有**动态生成的输入框（v2 的表单大半是 JS 拼出来的），
   只监听 childList，不监听 attributes —— 避免 setAttribute 触发自己造成死循环。 */
/* ⚠️ 必须节流（v3.0.3）。原来直接在 body 上挂 MutationObserver 且回调里
   `querySelectorAll('input,textarea,select')` 全量扫 —— 而列表每渲染一次
   就新增 25 个 checkbox、详情每打开一次就重建一整块表单。
   页面上有 57 个 input，等于每次渲染都全扫一遍。改成最多 200ms 一次。 */
var _KILL_T = null;
function killAutofill(){
  Array.prototype.forEach.call(
    document.querySelectorAll('input,textarea,select'), function(el){
      if(el.getAttribute('autocomplete') !== 'off') el.setAttribute('autocomplete','off');
    });
}
function killAutofillSoon(){
  if(_KILL_T) return;
  _KILL_T = setTimeout(function(){ _KILL_T = null; killAutofill(); }, 200);
}
if(window.MutationObserver){
  new MutationObserver(killAutofillSoon)
    .observe(document.body, {childList: true, subtree: true});
}

function pickAttach(){
  var f = document.getElementById('upAttach');
  if(f) f.click();
}
/* 附件预览（业务方 2026-10-21：「邮件附件没有预览」）。
   上传的是**临时附件**（outbox），原件在服务器暂存区，
   后端 `GET /api/mail/outbox/{id}?name=` 支持 inline 打开（`server.py:5304`），
   这里只要拼好 URL 交给浏览器新窗口打开即可 —— 原生导航带不了请求头，
   登录模式下靠 `withToken()` 补 `?token=`（与打开简历原件同一口径）。 */
function outboxOpen(oid, name){
  if(!oid) return;
  window.open(withToken('/api/mail/outbox/' + encodeURIComponent(oid)
    + '?name=' + encodeURIComponent(name || '')), '_blank');
}
/* 附件汇总。原来是**空函数占位**：界面上除了几行文件名没有任何说明，
   而且文件名不可点 —— 上传完根本没法确认"发出去的到底是哪份、多大"。
   现在给出份数与总大小，并提示可点开预览。 */
function renderMailAtt(){
  var sum = document.getElementById('mAttSum');
  if(!sum) return;
  if(!M_OUTBOX.length){ sum.textContent = ''; return; }
  var total = 0;
  M_OUTBOX.forEach(function(x){ total += Number(x.size) || 0; });
  /* 小于 1KB 的文件四舍五入会变成"0 KB"，看着像空文件 —— 改用字节数 */
  var human = !total ? '' : (total > 1048576 ? (total / 1048576).toFixed(1) + ' MB'
                            : (total < 1024 ? total + ' B' : Math.round(total / 1024) + ' KB'));
  sum.textContent = '将随这封邮件发出，共 ' + M_OUTBOX.length + ' 份'
    + (human ? '（合计 ' + human + '）' : '') + '；点文件名或「预览」可先看一眼内容。';
}

/* ---------- 临时附件上传 ---------- */
async function uploadOutbox(files){
  var list = Array.prototype.slice.call(files || []);
  if(!list.length) return;
  var msg = document.getElementById('upAttMsg');
  var existing = M_OUTBOX.reduce(function(n, x){ return n + (Number(x.size) || 0); }, 0);
  var adding = list.reduce(function(n, f){ return n + (Number(f.size) || 0); }, 0);
  if(existing + adding > 20 * 1024 * 1024){
    if(msg) msg.textContent = '附件合计超过 20MB，请压缩或减少文件后再上传';
    toast('附件合计超过 20MB，请压缩或减少文件', 'warn');
    return;
  }
  for(var i = 0; i < list.length; i++){
    if(msg) msg.textContent = '上传中…（' + (i + 1) + '/' + list.length + '）';
    var f = list[i];
    try{
      var r = await fetch(withToken('/api/mail/outbox?name=' + encodeURIComponent(f.name)),
        {method:'POST', headers: hdr({'Content-Type':'application/octet-stream'}), body: f});
      var b = await r.json();
      if(!r.ok || b.error){ throw new Error(b.detail || b.error || ('HTTP ' + r.status)); }
      M_OUTBOX.push({outbox_id: b.outbox_id, filename: b.filename, size: b.size});
    }catch(e){
      if(msg) msg.textContent = f.name + ' 上传失败：' + (e.message || e);
      renderOutbox();
      return;
    }
  }
  if(msg) msg.textContent = '已上传 ' + M_OUTBOX.length + ' 个附件';
  renderOutbox();
}
function renderOutbox(){
  var box = document.getElementById('mOutBox');
  if(!box) return;
  if(!M_OUTBOX.length){
    box.innerHTML = '<div class="hint">不带附件（纯文字邮件）</div>';
    renderMailAtt();
    return;
  }
  box.innerHTML = M_OUTBOX.map(function(x, i){
    var sz = Number(x.size) || 0;
    var kb = sz ? (' ' + (sz < 1024 ? sz + ' B' : Math.round(sz / 1024) + ' KB')) : '';
    /* 文件名本身可点（`title` 说明），再给一个显式的「预览」按钮 ——
       只在文字上下手，很多人不会想到去点文件名。 */
    return '<div class="doc">' +
        '<span class="doc-n">' +
          '<a href="javascript:void(0)" title="点击预览这个附件" ' +
            'onclick="outboxOpen(' + jq(x.outbox_id) + ',' + jq(x.filename || '') + ')">' +
            esc(x.filename || '') + '</a>' +
          '<span class="hint">' + kb + '</span></span>' +
        '<span class="doc-a">' +
          '<button class="btn sm" onclick="outboxOpen(' + jq(x.outbox_id) + ',' +
            jq(x.filename || '') + ')">预览</button>' +
          '<button class="btn sm" onclick="dropOutbox(' + i + ')">移除</button></span>' +
      '</div>';
  }).join('');
  renderMailAtt();
}
async function dropOutbox(i){
  /* 原来只做了 `M_OUTBOX.splice(i,1)` —— 只把这一行从界面上抹掉，
     暂存区里的文件还在、原版还会写一条 delete 审计。
     取消勾选不等于删除文件：要真删得后端动手，并且要说清删的是暂存副本。 */
  var x = M_OUTBOX[i];
  if(!x) return;
  var ok = await askConfirm({title: '移除附件', danger: true, okText: '移除',
    body: '移除附件「' + (x.filename || ('附件' + (x.outbox_id || ''))) + '」？'
        + '\n\n只删暂存区里的这份副本（已入库的简历原件不受影响），发送前还可以重新上传。'});
  if(!ok) return;
  if(x.outbox_id){
    var r = await api('/api/mail/outbox/' + encodeURIComponent(x.outbox_id) + '/delete',
      {method: 'POST'});
    if(r.__err){ toast(r.__err, 'danger'); return; }
  }
  M_OUTBOX.splice(i, 1);
  renderOutbox();
  var msg = document.getElementById('upAttMsg');
  if(msg) msg.textContent = '已上传 ' + M_OUTBOX.length + ' 个附件';
}

/* ---------- 生成草稿：把运行时变量交给服务端渲染 ---------- */
function collectRuntime(){
  var rt = {};
  (MAIL_VARS.runtime || []).forEach(function(v){
    var el = document.getElementById('mv_' + v.key);
    if(!el) return;
    var val = (el.value || '').trim();
    if(v.key === '面试时间' && el.dataset.weekday) val = val + '（' + el.dataset.weekday + '）';
    if(val) rt[v.key] = val;
  });
  return rt;
}
async function genDraft(){
  if(!M_CID){ toast('先在第一步选收件人'); return; }
  var tid = document.getElementById('mTpl').value;
  var body = {candidate_id: M_CID, application_id: M_AID, runtime: collectRuntime()};
  if(tid) body.template_id = Number(tid);
  var r = await api('/api/mail/draft', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if(r.__err){ toast(r.__err); return; }
  if(r.to) M_TO = r.to;
  document.getElementById('mSubject').value = r.subject || '';
  document.getElementById('mBody').innerHTML = r.body || '';
  var miss = r.missing || [];
  document.getElementById('mMiss').innerHTML = miss.length
    ? '<div class="miss">模板里还有待填：' + esc(miss.join('、')) + '</div>' : '';
  document.getElementById('mailSentBox').innerHTML = '';
  M_RESULT = null;
}

/* ---------- 富文本（原版表格编辑器，**原样移植**）---------- */
/* ---- v2 适配垫片：原版的弹层是 #modal/#mBody，v2 是 .mask/.dlg ---- */
function setModalTitle(t){ var e=document.getElementById('dlgTitle'); if(e) e.textContent = t; }
function closeModal(){
  closeProgDlg();
}
function openEditorDlg(){
  var m2=document.getElementById('mask');
  var plain=document.getElementById('dlgBody');
  if(plain) plain.style.display='none';
  document.getElementById('dlgNo').style.display='none';
  document.getElementById('dlgYes').style.display='none';
  var b=document.getElementById('edDlgBody');
  if(b){ b.style.display='block'; }
  if(m2) m2.classList.add('on');
  ensureDlgClose();
}
const EDITOR_TOOLBAR = (id) => `
  <div class="bar" style="margin-bottom:6px;flex-wrap:wrap;gap:4px 6px">
    <button onclick="editorCmd('bold','${id}')" title="加粗（Ctrl+B）"><b>B</b></button>
    <button onclick="editorCmd('italic','${id}')" title="斜体"><i>I</i></button>
    <button onclick="editorCmd('underline','${id}')" title="下划线"><u>U</u></button>
    <span class="vdiv"></span>
    <button class="btn-primary" onclick="insertTableDialog('${id}')">插入表格</button>
    <button onclick="tableAddRow('${id}')" title="在光标所在行下面加一行">＋行</button>
    <button onclick="tableDelRow('${id}')" title="删除光标所在行">－行</button>
    <button onclick="tableAddCol('${id}')" title="在末尾加一列">＋列</button>
    <button onclick="tableDelCol('${id}')" title="删除光标所在列">－列</button>
    <span class="vdiv"></span>
    <button onclick="tableMergeRight('${id}')" title="与右边单元格合并（版式里的跨列）">合并→</button>
    <button onclick="tableSplit('${id}')" title="把合并的单元格拆开一格">拆分</button>
    <span class="vdiv"></span>
    <button onclick="tableAlign('${id}','left')" title="左对齐">⇤</button>
    <button onclick="tableAlign('${id}','center')" title="居中">≡</button>
    <button onclick="tableAlign('${id}','right')" title="右对齐">⇥</button>
    <button onclick="beautifyTable('${id}')" title="一键统一边框/表头底色/行高，让表格整齐好看">表格美化</button>
    <button onclick="removeTableAtCaret('${id}')">删除表格</button>
  </div>`;

function richEditorHtml(id, minLines) {
  const h = minLines ? `min-height:${minLines}px;` : '';
  return `${EDITOR_TOOLBAR(id)}
    <div id="${id}" class="richeditor" contenteditable="true" style="${h}"
         oninput="onEditorInput('${id}')" onpaste="return onEditorPaste(event)"></div>`;
}

let EDITOR_TARGET = 'mBody';      // 对话框要操作哪个编辑器
let EDITOR_SEL = null;                 // 最后一次落在编辑器内的选区 {id, range}

document.addEventListener('selectionchange', () => {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return;
  let n = sel.anchorNode;
  if (n && n.nodeType !== 1) n = n.parentNode;
  const host = (n && n.closest) ? n.closest('.richeditor') : null;
  if (host) EDITOR_SEL = {id: host.id, range: sel.getRangeAt(0).cloneRange()};
});

function editorEl(id){ return document.getElementById(id || 'mBody'); }
function editorBody(id){
  const ed = editorEl(id);
  return ed ? ed.innerHTML : '';
}
/* 聚焦并**恢复上次光标**——不恢复的话"光标在哪个单元格"就丢了，
   加行/合并/对齐这些操作会作用到错误的位置（表现成"按钮没用"）。 */
function _focusEditor(id){
  const ed = editorEl(id || EDITOR_TARGET);
  if (!ed) return null;
  ed.focus();
  if (EDITOR_SEL && EDITOR_SEL.id === ed.id){
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(EDITOR_SEL.range);
  }
  return ed;
}

function editorCmd(cmd, id){
  const ed = _focusEditor(id);
  if (!ed) return;
  try { document.execCommand(cmd, false, null); } catch(e){ /* 浏览器不支持就静默 */ }
}

function onEditorInput(id){
  // 只做一件事：让"空编辑器"仍然是可点区域（不然点不进去）；
  // 不做实时预览——所见即所得，不需要第二块面板。
  const ed = editorEl(id);
  if (ed && !ed.innerHTML.trim()) ed.innerHTML = '<p><br></p>';
}

/* 插入表格：先问行列，再把真表格插到光标处。
   为什么不给骨架文本：那还得让 HR 记 `| --- | --- |`，漏了整张表退化成纯文本。
   直接插真表格，没有"语法漏写"这种失败模式。 */
function insertTableDialog(id){
  EDITOR_TARGET = id || 'mBody';
  setModalTitle('插入表格');
  document.getElementById('edDlgBody').innerHTML = `
    <div class="bar">
      <span class="small">行（含表头）</span>
      <input id="tblRows" type="number" value="4" min="1" max="30" style="width:80px">
      <span class="small">列</span>
      <input id="tblCols" type="number" value="2" min="1" max="10" style="width:80px">
      <button class="btn-primary" onclick="doInsertTable()">插入</button>
      <button onclick="closeModal()">取消</button>
    </div>
    <div class="small" style="margin-top:10px">插入后直接在格子里打字（第一行是表头）。
      要调版式：用工具栏的<b>＋行/－行/＋列/－列</b>改形状
      （光标不在表格里也没关系，会作用到最近那张表的末尾），
      用<b>合并→</b>做跨列（如整行标题、标签+内容），满意后点<b>表格美化</b>一键统一样式。</div>`;
  openEditorDlg();
}

function _tableHtml(rows, cols){
  const head = '<tr>' + Array.from({length: cols}, (_, c) =>
    `<th>${c === 0 ? '项目' : (c === 1 ? '内容' : '列' + (c + 1))}</th>`).join('') + '</tr>';
  const body = Array.from({length: Math.max(0, rows - 1)}, () =>
    '<tr>' + Array.from({length: cols}, () => '<td>&nbsp;</td>').join('') + '</tr>').join('');
  return `<table><thead>${head}</thead><tbody>${body}</tbody></table><p><br></p>`;
}

function doInsertTable(){
  const rows = Math.max(1, Math.min(30, parseInt(document.getElementById('tblRows').value, 10) || 4));
  const cols = Math.max(1, Math.min(10, parseInt(document.getElementById('tblCols').value, 10) || 2));
  const html = _tableHtml(rows, cols);
  closeModal();
  const ed = _focusEditor(EDITOR_TARGET);
  if (!ed) return;
  let done = false;
  try { done = document.execCommand('insertHTML', false, html); } catch(e){ done = false; }
  if (!done){
    // 兜底：把表格节点插到光标处（拿不到光标就追加到末尾）
    const sel = window.getSelection();
    const frag = document.createRange().createContextualFragment(html);
    if (sel && sel.rangeCount){
      const r = sel.getRangeAt(0);
      r.deleteContents(); r.insertNode(frag);
    } else {
      ed.appendChild(frag);
    }
  }
  beautifyTable(EDITOR_TARGET);            // 插完直接给一套整齐的样式，不用再点一次
  toast('已插入 ' + rows + ' 行 × ' + cols + ' 列，直接点单元格就能改', 'ok');
}

/* ------------------------- 表格排版操作（HR 的"调整空间"） -------------------------
   `execCommand` 管不了表格结构，这部分靠 DOM 直接改。
   每个操作都先 `_focusEditor` 恢复光标，再取"光标所在的单元格/行"，改完把光标
   放回原处——不然连点两下就会作用到别处，表现成"按钮随机失效"。

   为什么要这些按钮：院里的版式有**跨列**（整行标题、标签+内容）、需要加行减列。
   没有这些操作，HR 想改一点版式就只能去动 HTML 源码——那是把他逼回"看不懂的地方"。 */
function _cellAtCaret(id){
  const ed = _focusEditor(id);
  if (!ed) return null;
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return null;
  let n = sel.getRangeAt(0).startContainer;
  if (n && n.nodeType !== 1) n = n.parentNode;
  while (n && n !== ed){
    if (n.nodeType === 1 && (n.tagName === 'TD' || n.tagName === 'TH')) return n;
    n = n.parentNode;
  }
  return null;
}
/* 光标不在任何格子里时，找"离光标最近的那张表"。
   为什么要有它：HR 的常态是「插完表 → 直接点＋行」，不会先把光标塞进表格里点一下；
   原版一律弹「先把光标放到表格里」把人挡在门外 —— 实测就卡在这一步，按钮看着在、
   点下去没反应。**只要这一篇里有表格，就不该让按钮装死。**
   怎么算"最近"：按文档顺序取光标**前面**最近的那张（刚插的表通常在光标之前）；
   前面一张都没有，才退到后面最近的那张；选区压根不在编辑器里就取最后一张。 */
function _nearestTable(ed){
  const tbls = [...ed.querySelectorAll('table')];
  if (!tbls.length) return null;
  const sel = window.getSelection();
  let n = (sel && sel.rangeCount) ? sel.getRangeAt(0).startContainer : null;
  if (n && n.nodeType !== 1) n = n.parentNode;
  if (!n || !ed.contains(n)) return tbls[tbls.length - 1];
  let before = null, after = null;
  for (const t of tbls){
    const p = t.compareDocumentPosition(n);   // n 在 t 之前 → 含 PRECEDING(4)
    if (p & 4) before = t;                     // 循环后留下的是紧邻光标的上一张
    else if ((p & 2) && !after) after = t;      // FOLLOWING(2)：只留第一张
  }
  return before || after || tbls[tbls.length - 1];
}
/* 表格操作的统一入口：
   cell    = 光标所在格子；光标不在表里时给**最后一个格子**（够用来定"末行/末列"）
   tbl     = 要操作的表（光标所在表，或就近的那张）
   guessed = true 表示光标并不在表里，是替HR 猜的 —— 提示里要说清动的是哪张表的末行/末列，
             否则他会以为改的是自己刚点的那个位置。 */
function _tableCtx(id){
  const ed = _focusEditor(id);
  if (!ed) return null;
  const cell = _cellAtCaret(id);
  if (cell) return {cell: cell, tbl: cell.closest('table'), guessed: false};
  const tbl = _nearestTable(ed);
  if (!tbl) return {cell: null, tbl: null, guessed: false};
  const rows = [...tbl.rows];
  const last = rows[rows.length - 1];
  return {cell: last ? last.cells[last.cells.length - 1] : null,
          tbl: tbl, guessed: true};
}
/* 提示统一走这里：猜着干的要在文案里讲明白，否则等于让人猜自己改了什么 */
function _tblToast(msg, ctx, warn){
  toast(ctx && ctx.guessed ? (msg + '（光标不在表格里，已作用于最近那张表的末尾）')
                           : msg, warn ? 'warn' : 'ok');
}
function _placeCaret(el){
  try {
    const r = document.createRange();
    r.selectNodeContents(el);
    r.collapse(true);
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(r);
    EDITOR_SEL = {id: (el.closest('.richeditor') || {}).id, range: r.cloneRange()};
  } catch(e){ /* 放不回去也不影响已经做完的修改 */ }
}
function _colCount(tbl){
  const rows = [...tbl.rows];
  return rows.reduce((m, r) => Math.max(m, [...r.cells].reduce((s, c) =>
    s + (parseInt(c.getAttribute('colspan'), 10) || 1), 0)), 1);
}

function tableAddRow(id){
  const ctx = _tableCtx(id);
  if (!ctx || !ctx.cell){ toast('这里还没有表格，先点工具条的「插入表格」','warn'); return; }
  const row = ctx.cell.parentNode;
  const cells = [...row.cells];
  const isHead = cells[0].tagName === 'TH';
  const tr = document.createElement('tr');
  cells.forEach(c => {
    const td = document.createElement(isHead ? 'th' : 'td');
    const cs = parseInt(c.getAttribute('colspan'), 10) || 1;
    if (cs > 1) td.setAttribute('colspan', cs);
    td.innerHTML = '&nbsp;';
    tr.appendChild(td);
  });
  row.parentNode.insertBefore(tr, row.nextSibling);
  _placeCaret(tr.cells[0]);
  _tblToast('已加一行', ctx);
}
function tableDelRow(id){
  const ctx = _tableCtx(id);
  if (!ctx || !ctx.cell){ toast('这里还没有表格，先点工具条的「插入表格」','warn'); return; }
  const row = ctx.cell.parentNode;
  const tbl = row.closest('table');
  if (tbl.rows.length <= 1){ toast('只剩一行了，不能删；要整张删用「删除表格」','warn'); return; }
  const prev = row.previousElementSibling || row.nextElementSibling;
  row.remove();
  if (prev) _placeCaret(prev.cells[0]);
  _tblToast('已删一行', ctx);
}
function tableAddCol(id){
  const ctx = _tableCtx(id);
  if (!ctx || !ctx.tbl){ toast('这里还没有表格，先点工具条的「插入表格」','warn'); return; }
  [...ctx.tbl.rows].forEach(r => {
    const isHead = r.cells[0] && r.cells[0].tagName === 'TH';
    const c = document.createElement(isHead ? 'th' : 'td');
    c.innerHTML = '&nbsp;';
    r.appendChild(c);
  });
  _tblToast('已在末尾加一列（新列在最后一列，把它拖到想要的位置即可）', ctx);
}
function tableDelCol(id){
  const ctx = _tableCtx(id);
  if (!ctx || !ctx.cell){ toast('这里还没有表格，先点工具条的「插入表格」','warn'); return; }
  const tbl = ctx.tbl;
  if (_colCount(tbl) <= 1){ toast('只剩一列了，不能删','warn'); return; }
  const idx = [...ctx.cell.parentNode.cells].indexOf(ctx.cell);
  [...tbl.rows].forEach(r => { if (r.cells[idx]) r.cells[idx].remove(); });
  _tblToast('已删一列', ctx);
}
/* 与右格合并：版式里的"跨列"就是靠它做出来的（整行标题、标签+内容） */
function tableMergeRight(id){
  const ctx = _tableCtx(id);
  if (!ctx || !ctx.cell){ toast('这里还没有表格，先点工具条的「插入表格」','warn'); return; }
  const cell = ctx.cell;
  const next = cell.nextElementSibling;
  if (!next){ toast('右边没有格子了（已经是这一行最后一个）','warn'); return; }
  const a = parseInt(cell.getAttribute('colspan'), 10) || 1;
  const b = parseInt(next.getAttribute('colspan'), 10) || 1;
  cell.setAttribute('colspan', a + b);
  cell.setAttribute('rowspan', cell.getAttribute('rowspan') || 1);
  if ((cell.innerHTML || '').trim() === '') cell.innerHTML = next.innerHTML;
  next.remove();
  _placeCaret(cell);
  _tblToast('已与右边合并（跨 ' + (a + b) + ' 列）', ctx);
}
function tableSplit(id){
  const ctx = _tableCtx(id);
  if (!ctx || !ctx.cell){ toast('这里还没有表格，先点工具条的「插入表格」','warn'); return; }
  const cell = ctx.cell;
  const cs = parseInt(cell.getAttribute('colspan'), 10) || 1;
  if (cs <= 1){ toast('这个格子没有跨列，不需要拆','warn'); return; }
  cell.setAttribute('colspan', cs - 1);
  const nc = document.createElement(cell.tagName === 'TH' ? 'th' : 'td');
  nc.innerHTML = '&nbsp;';
  cell.parentNode.insertBefore(nc, cell.nextSibling);
  _placeCaret(nc);
  _tblToast('已拆出一格（跨列 ' + cs + ' → ' + (cs - 1) + '）', ctx);
}
function tableAlign(id, align){
  const ctx = _tableCtx(id);
  if (!ctx || !ctx.cell){ toast('这里还没有表格，先点工具条的「插入表格」','warn'); return; }
  ctx.cell.style.textAlign = align;
  _tblToast('该单元格已' + (align === 'center' ? '居中' : align === 'right' ? '右对齐' : '左对齐'),
            ctx);
}
/* 一键美化：统一**正文里所有表格**的边框 / 表头底色 / 行高 / 垂直居中。
   为什么处理全部而不是光标所在那一张：HR 说"让版式整齐"时指的是整篇，
   粘进来两三张表还要逐个点，等于没自动化。
   为什么要有它：手工给每个格子调样式既费劲又不一致；这里给一套克制的公文体，
   一次点好，之后还能单独改某个格子。样式写成**内联**——邮件客户端只认内联。 */
function beautifyTable(id, quiet){
  const ed = editorEl(id);
  if (!ed) return 0;
  const tables = [...ed.querySelectorAll('table')];
  if (!tables.length){ if (!quiet) toast('这里还没有表格','warn'); return 0; }
  const BORDER = '1px solid #bfbfbf';
  const CELL = `border:${BORDER};padding:6px 10px;vertical-align:middle`;
  tables.forEach(tbl => {
    tbl.setAttribute('style', 'border-collapse:collapse;width:100%;font-size:14px;'
      + "font-family:'Microsoft YaHei',sans-serif;margin:8px 0");
    tbl.setAttribute('cellspacing', '0');
    tbl.setAttribute('cellpadding', '0');
    const colCount = _colCount(tbl);
    [...tbl.rows].forEach((r, ri) => {
      const cells = [...r.cells];
      // 单格占满整行的行 = 标题行（如"面试邀请"）：居中、放大、稍加底色
      const span = parseInt(cells[0] && cells[0].getAttribute('colspan'), 10) || 1;
      const isTitle = cells.length === 1 && span >= colCount;
      cells.forEach(c => {
        const isHead = c.tagName === 'TH';
        if (isTitle){
          c.setAttribute('style', `border:${BORDER};padding:10px;text-align:center;`
            + 'font-size:18px;font-weight:600;background:var(--surface-2)');
        } else if (isHead || ri === 0){
          // 粘贴来的表格常全是 td（没有 th）：**首行一律当表头**，
          // 否则整张表一片白，看着就是"没排版"（保留原标签，不破坏 colspan 结构）
          c.setAttribute('style', `${CELL};text-align:center;font-weight:600;`
            + 'background:var(--surface-2)');
        } else {
          c.setAttribute('style', CELL);
        }
      });
    });
  });
  if (!quiet){
    toast('已统一 ' + tables.length + ' 张表格的样式（边框/表头底色/行高）；'
          + '想单独调某个格子，把光标放进去改即可', 'ok');
  }
  return tables.length;
}

/* 删除光标所在的那张表（不在表格里就提示，不误删正文） */
function removeTableAtCaret(id){
  const ed = editorEl(id);
  if (!ed) return;
  const cell = _cellAtCaret(id);
  let tbl = cell ? cell.closest('table') : null;
  if (!tbl){
    const all = ed.querySelectorAll('table');
    if (all.length === 1) tbl = all[0];            // 只有一张表：意图明确，直接删
  }
  if (!tbl){ toast('把光标放到要删的表格里再点这个按钮','warn'); return; }
  tbl.remove();
  toast('表格已删除（正文其他内容不受影响）','ok');
}

function clearEditor(id){
  const ed = editorEl(id);
  if (!ed) return;
  ed.innerHTML = '<p><br></p>';
  ed.focus();
  toast('已清空；内容已写的部分没保存的话就没了，注意别误点','warn');
}

/* 插入变量占位符（模板编辑用）：HR 不用记变量名，点一下就插到光标处。 */
function insertVar(id, key){
  const ed = _focusEditor(id);
  if (!ed) return;
  try { document.execCommand('insertText', false, '{' + key + '}'); }
  catch(e){ ed.innerHTML += '{' + key + '}'; }
}

/* 把当前正文存成模板：在编辑器里把版式调好（比如那张"面试邀请"表），
   一键存下来，下次选个人就能复用。
   **存的是 HTML**——复杂版式（合并单元格、居中大标题）用极简表格语法表达不了，
   存 HTML 才不会被简化掉；生成草稿时会原样渲染回来。 */
function onEditorPaste(ev){
  const host = ev.target && ev.target.closest ? ev.target.closest('.richeditor') : null;
  const hostId = host ? host.id : 'mBody';
  const html = (ev.clipboardData || window.clipboardData).getData('text/html');
  const text = (ev.clipboardData || window.clipboardData).getData('text/plain');
  if (!html){                                   // 纯文本粘贴：按原样、保留换行
    ev.preventDefault();
    document.execCommand('insertText', false, text || '');
    return false;
  }
  ev.preventDefault();
  const box = document.createElement('div');
  box.innerHTML = html;
  box.querySelectorAll('script,style,meta,link,iframe').forEach(n => n.remove());
  box.querySelectorAll('*').forEach(n => {
    [...n.attributes].forEach(a => {
      const keep = (n.tagName === 'TD' || n.tagName === 'TH') &&
                   ['colspan', 'rowspan'].includes(a.name.toLowerCase());
      if (!keep) n.removeAttribute(a.name);
    });
  });
  const hadTable = !!box.querySelector('table');
  const frag = document.createDocumentFragment();
  while (box.firstChild) frag.appendChild(box.firstChild);
  const sel = window.getSelection();
  if (sel && sel.rangeCount){
    const r = sel.getRangeAt(0);
    r.deleteContents(); r.insertNode(frag);
  }
  if (hadTable){
    const n = beautifyTable(hostId, true);     // 静默统一，下面给一句合并提示
    if (n) toast('已粘贴并按公文样式统一了 ' + n + ' 张表格（可再单独调格子）', 'ok');
  }
  return false;
}

/* ---------- 富文本 ---------- */
function fmt(cmd, editorId){
  /* editorId 默认是写邮件正文；模板编辑页传 'tplEditor'
     （原来写死 mBody，模板那边就没有富文本可用） */
  var box = document.getElementById(editorId || 'mBody');
  if(box) box.focus();
  try{ document.execCommand(cmd, false, null); }catch(e){ toast('当前浏览器不支持这个格式', 'warn'); }
}
/* 模板编辑器里插入变量占位符（与正文变量同一套 {变量} 写法） */
function tplInsertVar(k){
  if(!k) return;
  var ed = document.getElementById('tplEditor');
  if(!ed) return;
  ed.focus();
  try{ document.execCommand('insertText', false, '{' + k + '}'); }
  catch(e){ ed.innerHTML += '{' + k + '}'; }
}
/* 模板里插一张标准表格：录用通知/面试安排在院里基本都是表格形式 */
function tplInsertTable(){
  var ed = document.getElementById('tplEditor');
  if(!ed) return;
  ed.focus();
  var html = '<table border="1" cellspacing="0" cellpadding="6" ' +
    'style="border-collapse:collapse;width:100%">' +
    '<thead><tr><th>项目</th><th>内容</th></tr></thead>' +
    '<tbody><tr><td>&nbsp;</td><td>&nbsp;</td></tr>' +
    '<tr><td>&nbsp;</td><td>&nbsp;</td></tr></tbody></table><p><br></p>';
  try{ document.execCommand('insertHTML', false, html); }
  catch(e){ ed.innerHTML += html; }
}
/* ---------- 发送 ---------- */
async function checkSmtp(){
  var m = document.getElementById('smtpMsg');
  if(m) m.textContent = '检查中…';
  var r = await api('/api/mail/test-smtp', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: '{}'});
  if(m) m.textContent = r.__err ? ('✗ ' + r.__err) : ('✓ ' + (r.message || '发信配置可用'));
}
async function sendMail(){
  if(M_SENDING){ toast('邮件正在发送，请勿重复提交', 'warn'); return; }
  var manual = document.getElementById('mToManual');
  var to = manual ? (manual.value || '').trim() : M_TO;
  var subject = (document.getElementById('mSubject').value || '').trim();
  var html = (document.getElementById('mBody') || {}).innerHTML || '';
  var text = html.replace(/<[^>]*>/g, '').trim();
  if(!to || to.indexOf('@') < 0){ toast('请先确认收件人邮箱', 'warn'); return; }
  if(!subject){ toast('请填邮件主题', 'warn'); return; }
  if(!text && html.indexOf('<table') < 0 && html.indexOf('<img') < 0){
    toast('正文是空的', 'warn'); return;
  }
  var totalBytes = M_OUTBOX.reduce(function(n, x){ return n + (Number(x.size) || 0); }, 0);
  if(totalBytes > 20 * 1024 * 1024){
    toast('附件合计 ' + (totalBytes / 1048576).toFixed(1) +
          'MB，超过邮件上限 20MB；请压缩或减少附件', 'warn');
    return;
  }
  /* 确认框里把**附件名列出来**：只写"附件 2 个"时，人没法判断该不该发。 */
  var attNames = M_OUTBOX.map(function(x){ return x.filename || ('附件' + (x.outbox_id || '')); });
  var okC = await askConfirm({title: '发送确认', danger: true, okText: '确认发送',
    body: '收件人：' + to + '\n主题：' + subject +
          (attNames.length ? '\n附件 ' + attNames.length + ' 个：' + attNames.join('、') : '') +
          '\n\n发出后无法撤回。'});
  if(!okC) return;
  M_MAIL_META = {to: to, subject: subject};
  if(!M_MAIL_REQ_ID) setMailRequestId(newMailRequestId());
  setMailSending(true);
  var r = await api('/api/mail/send?async=1', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      client_request_id: M_MAIL_REQ_ID,
      to: to, subject: subject, body: '', html: html,
      candidate_id: M_CID, application_id: M_AID,
      template_name: (MAIL_TPL.filter(function(t){
        return String(t.id) === document.getElementById('mTpl').value;
      })[0] || {}).name || '',
      /* 结构化运行时变量：服务端按**模板用途**回写面试/录用信息，
         条件是 `ok and application_id and vars`（`server.py:5444`）。
         原来根本不传 vars → 那段恒不成立 → 面试邀请发出去了，
         候选人的阶段与录用信息**没有落库**，后面看板还是老状态。 */
      vars: collectRuntime(),
      /* `filename2` 才是附件名：服务端取 `_a.filename2 or _a.filename or "附件"`
         （`server.py:5386`）。原来只给 outbox_id → 收到的附件全部叫「附件」。 */
      attachments: M_OUTBOX.map(function(x){
        return {outbox_id: x.outbox_id, filename2: x.filename};
      })
    })});
  if(r.__err){
    setMailSending(false);
    var resumed = await resumeMailTask();
    if(!resumed){
      toast('网络中断，暂时无法确认发送结果；请稍后重试。系统会复用本次请求，不会重复发送。', 'warn');
    }
    return;
  }
  if(r.task_id){
    attachMailTask(r.task_id, {meta: {to: to, subject: subject}});
    return;
  }
  finishMailSent(r);
}
/* 原先这里还有 openOutbox / delOutbox 两个函数，挂在「发件记录」的查看/删除按钮上。
   已删除（2026-10-20）：**后端根本没有"发件记录"这个概念** ——
   `send_mail` 的返回里没有 outbox_id（`mail_send.py:299-304`），而
   `/api/mail/outbox/{id}/delete` 删的是**临时上传附件的暂存文件**，不是发件留档。
   按钮文案写着「删除发件记录」、实际删的是附件，属于会误导人的假功能；
   又因为 outbox_id 恒为 undefined，那两个按钮压根没机会渲染。
   现在 renderSent 改为如实展示服务端真实回执（发件人 / 实际通道 / 已带附件 / 自动回写字段）。 */
function renderSent(){
  var box = document.getElementById('mailSentBox');
  if(!box || !M_RESULT) return;
  if(!M_RESULT.ok){
    box.innerHTML = '<div class="miss">发送失败：' + esc(M_RESULT.msg) + '</div>';
    return;
  }
  /* ⚠️ 这里原来渲染的是「发件记录 + 查看/删除」按钮，但**后端根本没有发件记录这个概念**：
     `send_mail` 返回里没有 outbox_id（`mail_send.py:299-304`），而 `/api/mail/outbox/…/delete`
     删的是**临时上传的附件**（暂存区），不是发件留档 —— 按钮文案与行为不符，
     而且因为 outbox_id 恒为 undefined，按钮压根不会渲染。
     改成如实展示服务端真实回执：实际发件人、真正走通的通道、实际带上的附件、
     以及自动回写进了档案的字段（HR 不用再自己去核对一遍）。 */
  var r = M_RESULT.res || {};
  var used = r.used || {};
  var atts = r.attachments || [];
  var written = Object.keys(M_RESULT.res_written || {});
  box.innerHTML =
    '<div class="sec" style="margin-top:0"><h3>已发送</h3>' +
      '<div class="ev"><b>' + esc(M_RESULT.to) + '</b>　' + esc(M_RESULT.subject) +
      '<br>' + esc(M_RESULT.at) + '</div>' +
      (r.from_header
        ? '<div class="bar2"><span class="lbl2">发件人</span>' +
          '<span class="num">' + esc(r.from_header) + '</span></div>' : '') +
      (used.host
        ? '<div class="bar2"><span class="lbl2">实际通道</span>' +
          '<span class="num">' + esc(used.host + ':' + used.port + ' ' + (used.mode || '')) +
          '</span></div>' : '') +
      (atts.length
        ? '<div class="bar2"><span class="lbl2">已带附件</span><span>' +
          esc(atts.join('、')) + '</span></div>' : '') +
      (written.length
        ? '<div class="bar2"><span class="lbl2">已自动记录</span><span>' +
          esc(written.join('、')) + '</span></div>' : '') +
      ((M_RESULT.missing || []).length
        ? '<div class="miss" style="margin-top:8px">有 ' + M_RESULT.missing.length +
          ' 个附件没发出去：' + esc(M_RESULT.missing.join('、')) +
          '（请在档案里重新上传原件，或去掉附件单独补发）</div>' : '') +
      (r.note ? '<div class="hint" style="margin-top:8px">' + esc(r.note) + '</div>' : '') +
    '</div>';
}
/* 把当前正文存成模板（原版功能） */
async function saveAsTemplate(){
  var subject = (document.getElementById('mSubject').value || '').trim();
  var html = (document.getElementById('mBody') || {}).innerHTML || '';
  if(!subject || !html){ toast('主题和正文都要有内容'); return; }
  var name = await askText('模板名称', subject.slice(0, 20), '给这个模板起个名字');
  if(!name || name === true) return;
  var r = await api('/api/mail/templates', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: name, subject: subject, body: html})});
  if(r.__err){ toast(r.__err); return; }
  toast('已存为模板：' + name);
}

document.getElementById('chkAll').addEventListener('change', function(e){
  setAll(e.target.checked);
});

killAutofill();

/* 启动（v3.0.3）：先决定"能不能进"，再决定"进来看什么"。
   登录模式下没有 token 就直接弹登录页，**一个业务请求都不发** ——
   否则控制台会先刷满一串 401，用户看到的是"页面坏了"而不是"请登录"。 */
document.getElementById('loginForm').addEventListener('submit', function(ev){
  ev.preventDefault();
  var u = document.getElementById('loginUser').value.trim();
  var p = document.getElementById('loginPass').value;
  if(u && p) doLogin(u, p);
});
(function boot(){
  /* 先按"当前还不知道是谁"收紧界面：账号管理入口藏起来、应用区压暗。
     登录成功后再由 renderWhoami() 放开。**不能等 loadMe() 回来才做** ——
     那一瞬间里侧栏是全亮的，看起来像没生效。 */
  applyPermissions();
  if(!AUTH_ENABLED){            /* 本机模式：原来的启动路径，一字不改 */
    load();
    /* 右侧默认展示「概览 · 待办」（业务方 2026-10-21）：没人选中时不该只留一句
       "左侧选一个人" —— 那块地方占着半屏，正好放今天该干什么。 */
    showOverview();
    return;
  }
  if(!TOKEN){ showLogin(''); return; }
  /* 有 token 但可能已失效：先问 /api/me。失效时 api() 会自动弹登录页，
     所以这里不需要额外的分支。 */
  loadMe().then(function(){
    if(!ME){ showLogin('登录已失效，请重新登录'); return; }
    renderWhoami();
    load();
    showOverview();
  });
})();
</script>
</body>
</html>
"""


def render_page_v2(auth_enabled: bool = False) -> str:
    """渲染 v2 预览页（注入阶段/档位配置，与旧版同一份真源）。"""
    stages_json = json.dumps({
        "order": stages_mod.all_stages(),
        "default_stage": stages_mod.DEFAULT_STAGE,
        "colors": {s: stages_mod.stage_color(s) for s in stages_mod.all_stages()},
        # v2.19：结论标签是**按阶段限定**的（待面试/已面试→拟录用·不录用，已发放offer→已签约·拒签），
        # 而「拟录用/不录用/已签约」这些结论在原版里是**阶段动作**的一部分。
        # 不注入 actions/outcomes，界面就只能给出一个固定的、未判/拟录用/不录用 下拉 ——
        # 那正是"改了不生效"的根因：服务端按阶段校验值域，前端给的选项压根不合法。
        "outcomes": (stages_mod.load_cfg().get("outcomes") or {}),
        "actions": (stages_mod.load_cfg().get("actions") or {}),
    }, ensure_ascii=False)
    tiers_json = json.dumps({
        "order": tiers_mod.all_tiers(),
        "labels": tiers_mod.labels(),
    }, ensure_ascii=False)
    # 构建时间：页脚显示"v2 预览 · 10-21 15:40"，方便判断浏览器跑的是不是最新代码。
    # 用本文件的修改时间，重启服务后自然更新。
    import datetime as _dt
    import os as _os
    try:
        _mt = _os.path.getmtime(__file__)
        _build = _dt.datetime.fromtimestamp(_mt).strftime("%m-%d %H:%M")
    except OSError:
        # PyInstaller 把模块放进 PYZ 时 __file__ 是虚拟路径，不能拿来 stat。
        from .version import VERSION

        _build = f"v{VERSION}"
    html = _PAGE.replace(_PH_STAGES, stages_json).replace(_PH_TIERS, tiers_json) \
            .replace(_PH_AUTH, "true" if auth_enabled else "false") \
            .replace("__BRAND_ICON__", _brand_icon_data_uri()) \
            .replace("__V2_BUILD__", _build)
    return html


def _brand_icon_data_uri() -> str:
    """侧栏品牌图标：icon_tray.png 缩到 96px 的 base64 data URI。

    为什么内联而不是挂个 /logo.png：
      · 页面本来就是单文件内联的，多一个请求在服务刚起时还会闪一下空图；
      · 打包后程序目录是 `_internal`，多一个"要跟着 spec 一起带上"的静态资源，
        就多一个会漏的地方（图标丢了下场是页面上一个裂图）。
    找不到图就退回空串——`<img src="">` 只会显示 alt（空），布局不塌。
    """
    import base64
    import io
    import os as _os

    from .paths import BASE

    # 三个候选位置，覆盖三种跑法：
    #   BASE/                —— 源码开发时把图标放在 repo_src 下
    #   BASE 的上一级        —— 源码开发时图标在工作区根（当前就是这种）
    #   BASE/_internal/      —— 打包后 BASE 已经是 _internal，图标跟 exe 同级
    here = _os.path.dirname(BASE)
    png = ""
    for cand in (_os.path.join(BASE, "icon_tray.png"),
                 _os.path.join(here, "icon_tray.png"),
                 _os.path.join(BASE, "_internal", "icon_tray.png")):
        if _os.path.isfile(cand):
            png = cand
            break
    if not png:
        return _BRAND_ICON_FALLBACK
    try:
        from PIL import Image

        with Image.open(png) as im:
            im = im.convert("RGBA")
            if im.size[0] > 96:
                im = im.resize((96, 96), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        # 缩不了就直接内联原图：宁可页面大 50KB，也不要品牌区空一块。
        try:
            with open(png, "rb") as fh:
                return "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
        except OSError:
            return _BRAND_ICON_FALLBACK


#: 品牌图标的**兜底**（v3.1.4）：一个内联 SVG，不依赖任何文件或 PIL。
#: 为什么必须有它：现场出现过侧栏左上角显示"裂图"——`<img src="">` 在浏览器里
#: 就是裂图占位符，比没有图标更难看。现在无论 PNG 能不能读出来，
#: 最差也是这个蓝色圆角方块 + 白色 "H"，布局和视觉都不会塌。
_BRAND_ICON_FALLBACK = (
    "data:image/svg+xml;charset=utf-8,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 96 96'%3E"
    "%3Crect width='96' height='96' rx='22' fill='%23005bdf'/%3E"
    "%3Cpath d='M26 70V26h10v17h24V26h10v44H60V53H36v17z' fill='%23fff'/%3E"
    "%3C/svg%3E"
)
