"""工作台界面：单页应用（HTML + 原生 JS，无构建步骤、无外部 CDN）。

九个视图：人才库（顶部内嵌投递管道，默认折叠一行人数） / 归档 / 智能助手 / 导入与来源 / 岗位管理 /
提案与审计 / 检索 / 邮箱配置 / 系统说明（红线承诺集中展示在「系统说明」页）。

产品定位（v2）：**只给 HR 使用**，单角色、无盲筛、无角色切换。

界面上必须始终可见的三条承诺（对应设计红线）：

1. 系统只给建议，HR 确认才生效；
2. 不淘汰、不删除任何简历；
3. 智能体的写操作一律以"待确认提案"呈现，不会自动落库。

岗位采用「停用而非删除」；邮件标题带岗位名时自动归岗，否则标「待指定」。
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache

#: 应用根目录（打包后是 exe 同级的 _internal：PyInstaller 会把 app/ 放进去）
from .paths import BASE as _BASE       # 根目录唯一来源（认 TP_HOME）

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="tp-build" content="__UI_BUILD__">
<title>企业人才库智能体</title>
<style>
  /* Hallmark · macrostructure: dense-tool · 单强调色（墨绿）· 纸墨中性 · 分隔线优先于卡片堆叠
     Hallmark · pre-emit critique: P4 H4 E4 S4 R4 V4
     刻意违反 Gate 1（display font 不用 system default）：本机只有雅黑/等线、无中文衬线体；
     高密度内部工具里字的可读性优先于"不像 AI"。身份感由色彩与结构承担。*/
  :root{
    /* === 配色：飞书观感，但色值不照抄飞书官方 ===
       HR 反馈"太素"，要求参考飞书。飞书的观感来自三件事，**不是**来自某个色号：
       ① 页面底是浅灰、卡片是纯白，靠**明度差**把卡片"托"起来；
       ② 主色是饱和的蓝，用在选中/强调/可点；
       ③ 中性色偏冷（#F5F6F7 / #DEE0E3 这一族），不用暖灰。
       之前那版是**暖灰纸墨**（#f5f5f4 / #e4e2dd），对比度没问题，但整体偏黄偏闷，
       加上全靠边框区分面，视觉上"平"。现在换成冷灰，卡片自然就立起来了。

       为什么色值不直接抄飞书：实测飞书官方色**大部分不能用作正文文字**——
       主蓝 #3370FF 对白只有 4.28、文字弱色 #8F959E 只有 3.02、
       绿 #34C724 只有 2.25。飞书自己主要把它们用在图形和大字上。
       我们的界面是高密度内部工具，13~14px 小字遍地，必须守住 4.5。
       所以：**观感照飞书，色值按我们的可读性底线重新配**，
       每个值都算过对比度（数字见注释）。 */
    --paper:#f5f6f7;        /* 页面底：飞书冷灰。卡片靠"比它亮"浮起来 */
    --surface:#ffffff;      /* 卡片/面板：纯白，与页面底差 1.08，形成明度层次 */
    --surface-2:#f2f3f5;    /* 次级面（表头、代码块、hover）：比页面底略深一档 */
    /* 侧栏单独一档，比页面底**明显更深**（1.10）。
       为什么不能复用 --surface-2：它是 #f2f3f5，与页面底 #f5f6f7 只差 1.03，
       肉眼看不出区别，侧栏就和内容区糊在一起了。
       一身兼两职的变量最容易被误用到拿不到层级的地方。 */
    --surface-nav:#e9ebee;
    --ink:#1f2329;          /* 主文字（对白 15.78，对页面底 14.58） */
    --ink-2:#51565d;        /* 次要文字（对白 7.40） */
    --ink-3:#646a73;        /* 弱化文字（对白 5.45；飞书的 #8F959E 只有 3.02，不可用） */
    --line:#dee0e3;         /* 分隔线（对白 1.32） */
    --line-2:#c9cdd4;       /* 强分隔线：需要"看得见边界"时用 */
    /* 唯一强调色：**飞书蓝**。面积仍然控制在 5% 以内——
       只给「可点 / 当前 / 进行中」，卡片与 chip 一律中性。 */
    --accent:#3370ff;       /* 主蓝：边框、选中线、图标（对白 4.28，用于图形与大字） */
    --accent-ink:#1d5fd6;   /* 深蓝：所有**文字**形态（链接、按钮字），对白 5.74 */
    --accent-soft:#f0f5ff;  /* 浅蓝底：按钮底、选中底 */
    --accent-line:#ccdcff;  /* 蓝描边：对浅蓝底 1.26 */
    --accent-hi:#e1ecff;    /* 蓝 hover 底：比 soft 再深一档，hover 要"有反应" */
    --focus:#1d5fd6;        /* 焦点环 */
    /* 状态色：同样照飞书观感重新配，每个都满足文字 4.5。
       -soft 是**底色**，-line 是**描边**，-xxx 是**文字**。
       描边对底色的对比度定在 **1.15~1.30**：低于 1.1 边界发糊等于没画，
       高于 1.4 描边会跳到文字前面抢视线。 */
    --ok:#1f7a3d; --warn:#a85b00; --bad:#c93a2e;
    --ok-soft:#e8f5eb; --warn-soft:#fff3e0; --bad-soft:#ffefee;
    --ok-line:#b8dfc4; --warn-line:#f5d5a8; --bad-line:#f5cfcb;
    /* 档位色：与状态色分开一套，因为表达的**不是**状态。
       B 档原来直接借用 --accent（主蓝），现在主蓝对白只有 4.28，
       达不到 4.5（旧的 #3d8fd1 更只有 3.47，是长期遗留的问题）。
       这里给 B 档一个专属的深青 #146b9e：对白 5.79，
       且与主蓝（1.35）、A档绿（1.08）都能明显区分，不会混作一档。
       A 档则**继续复用 --ok 绿**，不给它单独配色：试过 #0e7c4a 一类的深翠绿，
       和状态绿对比度只有 1.02~1.09，肉眼分不出，两套几乎一样的绿反而更乱。
       而且 A 档语义就是"最优/推荐"，与状态绿"没问题"本来就同向，共用合理。 */
    --tier-b:#146b9e; --tier-b-soft:#e8f1f8;
    /* 圆角：飞书比之前稍圆（卡片 8px / 控件 6px）。
       微圆角在中文界面里容易显得"像素对齐"，飞书用略圆的角来软化密集信息。 */
    --r-card:8px; --r-ctl:6px; --r-chip:4px;
    /* ⚠️ --z 必须和下面 body{zoom} 同步改，两处是一个整体。
       原因：zoom 放大的是**布局像素 → 视觉像素**，
       而 100vh 拿到的是**视觉像素**的视口高。于是 body 有 zoom 时，
       height:100vh 的元素会变成 视口高 × zoom，**比窗口高出整整一个 zoom 倍**——
       表现为右侧一条多余的文档滚动条、页面下方截掉一截（用户 2026-10-13 反馈）。
       所以凡是要"正好一屏"的高度，一律写 calc(100vh / var(--z))，
       **不要写死减多少 px**：写死的值换个窗口高度/缩放档就失效
       （实测 87vh 在 1000px 高窗口下碰巧正确，换1440 高立刻差 187px）。*/
    --z:1;
  }

  /* Gate 34：窄屏不允许横向滚动。clip 而非 hidden——后者会破坏 sticky/fixed */
  html,body{overflow-x:clip}
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);
       font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
       font-size:15px;line-height:1.7}
  /* 大屏适配（v1.7.1）：笔记本（<1600 宽）保持原样；24 寸及以上显示器整体放大，
     解决"内容挤在中间一圈留白、字号显小"的问题。
     为什么用 zoom 而不是逐个改字号：这套界面里几十处 px 字号（按钮 14、表格 14、
     说明文字 13、徽标 13……），逐个写媒体查询覆盖改动面大且必然漏；
     zoom 让字号、卡片、侧栏、弹窗**一起**按比例放大且布局随缩放重排，
     Chromium 与 Firefox 126+ 都支持。
     ⚠️ 每加一档 zoom，**必须同时把 --z 也设成同一个值**，
     否则 .layout / .side 的"正好一屏"会高出 zoom 倍（见 :root 里 --z 的说明）。*/
  @media (min-width:1600px){ body{zoom:1.15} :root{--z:1.15} }
  @media (min-width:2000px){ body{zoom:1.3}  :root{--z:1.3} }
  /* 侧栏：比内容区**略深**一档的灰（--surface-2）而不是纯白。
     飞书就是这样分层的——导航区沉一点，内容区浮起来。
     之前侧栏是纯白、body 也是纯白，两边一样亮，界面就没了纵深。 */
  /* v1.24.20：.layout 改成**固定一屏**（原min-height:100vh）。
   原来用 min-height 是对的（内容长了就长高、页面整体滚动），
   但那样聊天页无法把对话区撑到视区底部 —— 一屏之内放不下就该页内滚，
   不该让整页滚动。改成 height + overflow:hidden，
   滚动交给下层各页自己决定（.main-inner 或 .chatlog）。
   覆盖写在这一行而不是另起一条：CSS 同优先级按出现顺序生效，
   写在别处的覆盖会被原始定义盖掉（v1.24.20 踩过）。

   ⚠️ v1.24.21 修：这里是 calc(100vh / var(--z)) 而不是 height:100vh。
   body 上有 zoom（≥1600 宽时 1.15/1.3），100vh 是**视觉像素**视口高，
   而 .layout 的布局盒会被 zoom 乘一遍 —— 于是它比窗口高出 15%/30%，
   表现为右侧一条多余的文档滚动条、聊天页下方被截掉一截。
   实测（1600×1000、zoom1.15）：documentElement.scrollHeight=1150、innerHeight=1000，
   多出正好 150px = 1000×0.15。overflow:hidden 裁的是 .layout **内部**，
   裁不掉它自己超出视口的那一段。
   另外验证过 html,body{overflow:hidden} 单独加**完全无效**——
   溢出的是 .layout 这个元素本身，不是 body，所以别指望锁 body 能解决。 */
  .layout{display:flex;height:calc(100vh / var(--z));overflow:hidden;align-items:stretch}
  .side{width:220px;flex:0 0 220px;background:var(--surface-nav);border-right:1px solid var(--line);
    padding:20px 12px 14px;position:sticky;top:0;height:calc(100vh / var(--z));
    display:flex;flex-direction:column;box-sizing:border-box}
  .brand{display:flex;align-items:center;gap:10px;font-size:15px;font-weight:600;
    padding:4px 10px 18px;white-space:nowrap}
  .brand img.logo{width:30px;height:30px;border-radius:var(--r-ctl);flex:0 0 auto;display:block}
  .logo{width:30px;height:30px;border-radius:var(--r-ctl);background:var(--accent);color:var(--surface);
    display:flex;align-items:center;justify-content:center;font-size:15px;flex:0 0 auto}
  .nav{flex:1;overflow:auto}
  .navitem{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:var(--r-ctl);
    cursor:pointer;color:var(--ink-2);font-size:14px;margin-bottom:2px;white-space:nowrap}
  .navitem svg{width:17px;height:17px;flex:0 0 auto}
  /* 侧栏 hover 用**白底**：侧栏自身已经是 --surface-2 灰，再拿同一个灰当 hover
     等于点了没反应。飞书就是hover 时浮出一块白。 */
  .navitem:hover{background:var(--surface);color:var(--ink)}
  .navitem.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:500}
  .sidefoot{font-size:12px;color:var(--ink-3);padding:10px 12px 0;border-top:1px solid var(--line)}
  /* v1.24.20：.main 变flex 列，滚动交给 .main-inner。
   内容区不再自己滚动，而是撑满一屏、超出的部分由子容器滚。

   ⚠️ height:100% 不是冗余：.layout 是 align-items:stretch，
   .side 有 height:100vh，而 .main 作为 flex item 拿到的是**内容撑出来的高度**，
   不写 height:100% 时它的子级flex:1 参照的是"父级自动高度"——于是链条断在
   .main → .main-inner 之间，.chatlog 撑不出高度，输入框被顶到视口外
   （实测过：截图里对话区在滚，输入条看不见）。
   显式 height:100% 把这条链一次接到底。 */
  .main{flex:1;min-width:0;height:100%;padding:24px 30px 60px;
    display:flex;flex-direction:column;overflow:hidden}
  /* 聊天页不需要底部 60px 留白（内容要贴到可视区底部才叫"填满"），只留24px 呼吸位。 */
  body.main-on-chat .main{padding-bottom:24px}
  /* max-width 居中 + flex 列 + 自己滚：常规页面（人才库等）内容长就滚这里。
     ⚠️ 隐藏的兄弟节点（#stats 隐藏、#alerts 空）不占高度，flex 分配才对；
     #view 拿 flex:1 的全部剩余空间。 */
  .main-inner{max-width:1240px;margin:0 auto;width:100%;height:100%;
    flex:1;min-height:0;display:flex;flex-direction:column;overflow:auto}
  /* 聊天页把滚动从 .main-inner 移到 .chatlog 内部，否则底部会多出一截空白。
     body.main-on-chat 由 refresh() 一处切换（它才是渲染每一页的必经之路）。 */
  body.main-on-chat .main-inner{overflow:hidden}
  #view{flex:1;min-height:0;display:flex;flex-direction:column}
  #view > .panel:last-child{margin-bottom:0}
  h1{font-size:22px;font-weight:600;margin:0}
  h2{font-size:17px;font-weight:600;margin:0 0 12px}
  .sub{color:var(--ink-3);font-size:13px;margin-top:4px}
  .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;line-height:1.5}
  .vdiv{width:1px;height:22px;background:var(--line);display:inline-block;flex:none}
  .glbl{font-size:13px;color:var(--ink-3);flex:none}
  input,select,textarea{padding:7px 12px;border:1px solid var(--line);border-radius:var(--r-ctl);
    font-size:14px;color:var(--ink);background:var(--surface);font-family:inherit}
  button{padding:6px 14px;border:1px solid var(--line);border-radius:var(--r-ctl);background:var(--surface);
    font-size:14px;color:var(--ink);cursor:pointer;font-family:inherit}
  /* Gate 26/39：交互元素必须八态齐全。这里补齐最关键的 focus-visible——
     原来键盘 Tab 过去**完全看不见焦点**，对键盘用户等于不可用。
     focus 用 outline 而不是 border（border 会改变几何、引起布局跳动）。 */
  button:focus-visible,select:focus-visible,input:focus-visible,textarea:focus-visible,
  [tabindex]:focus-visible,.tab:focus-visible,.navitem:focus-visible{
    outline:2px solid var(--focus);outline-offset:1px}
  button:active{transform:translateY(.5px)}
  button:hover{color:var(--accent);border-color:var(--accent-line);background:var(--accent-hi)}
  button:disabled{opacity:.5;cursor:not-allowed}
  /* 主按钮：浅蓝底 + 深蓝字。
     保留这个做法是因为 HR 反馈过"深底浅字看不清"（国产字体渲染下更糊），
     实测后定的浅底深字。
     2026-10-12 配飞书时复算过：实心蓝底白字要把底色压到 #2b5fe0 才够4.5，
     而那已经是**另一个蓝**了，和作为强调色的 #3370ff 不是同一个值——
     "主按钮的蓝"和"强调的蓝"一旦不同色，界面会显得不干净。
     现在深蓝字 #1d5fd6 对浅蓝底 5.25，比实心方案更清楚，且不用引入第二个蓝。 */
  .btn-primary{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-ink)}
  .btn-primary:hover{background:var(--accent-hi);border-color:var(--accent-ink);color:var(--accent-ink)}
  .btn-ok{background:var(--ok);border-color:var(--ok);color:var(--surface)}
  .btn-ok:hover{background:var(--ok);border-color:var(--ok);color:var(--surface)}
  .btn-danger{background:var(--surface);border-color:var(--line-2);color:var(--bad)}
  .btn-danger:hover{background:var(--bad-soft);border-color:var(--bad);color:var(--bad)}
  td button{padding:3px 10px;font-size:13px}
  /* 候选人卡片：整张卡按档位在**左边**留一条 3px 色条。
     为什么放左边而不是只靠右上角的徽章：色条是"位置编码"，扫一列卡片时
     眼睛沿同一条线下来就知道哪几张是 A 档，不需要逐张去读徽章上的字。
     色条只表达档位这一个维度，宽度恒定 3px——一旦它开始承载第二个信息
     （比如"是否已确认"），就会退化成又一层需要解码的图形。 */
  /* 卡片/面板：白底 + **极淡的投影**。
     这是从"素"到"不素"最关键的一步：光靠 1px 边框，卡片和灰底之间只有
     明度差，边界是"画"出来的；加一层4%~6% 透明度的投影后，卡片变成
     "浮"在底上，纵深立刻出来。
     投影刻意压得很淡（飞书约 0 1px 2px + 0 0 1px rgba(31,35,41,.05)），
     不要再重——一重就变成拟物，卡片彼此打架，反而更乱。 */
  .card{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--line-2);
    border-radius:var(--r-card);padding:16px 18px;margin-bottom:12px;
    box-shadow:0 1px 2px rgba(31,35,41,.04)}
  .panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-card);
    padding:18px 20px;margin-bottom:14px;box-shadow:0 1px 2px rgba(31,35,41,.04)}

  /* 统计区（v1.24.14 优化）：原来 10 个数字**视觉权重完全相同**，都是 26px。
     问题不在难看，而在**没有重点**——HR 进门最该先看的是「待 HR 确认」和「A 档人数」，
     可它们和「简历附件数」一样大，眼睛不知道从哪看起。
     现在分两层：**待办层（需要你动手的）用大号数字**，
     **库存层（背景信息）缩小并弱化**，A/B/C/D 四档收成一条比例条。
     这不是装饰，是把"该看哪里"写进视觉层级里。 */
  .grid-stats{margin:18px 0 4px}
  /* 统计区（v1.25.6 二次重排）：上下两行用**同一套 4 列网格**——
     行与行的列沿严格对齐；字号放大一档；右侧环跨两行。
     不做成卡片、以细竖线分列（内部工具的数据表观感）。 */
  .stat-act{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0 20px;
    border-bottom:1px solid var(--line-2);padding-bottom:12px;margin-bottom:12px}
  .stat-act .stat{padding:4px 16px 4px 0}
  .stat-act .stat + .stat{border-left:0;padding-left:16px;border-left:1px solid var(--line)}
  .stat-act .stat .k{font-size:13px;letter-spacing:.02em}
  .stat-act .stat .v{font-size:26px;font-weight:600;line-height:1.2;
    font-variant-numeric:tabular-nums;letter-spacing:-.01em}
  /* 为 0 时不该吸引注意——"没有待确认"是好消息，不是警报 */
  .stat-act .stat.is-zero .v{color:var(--ink-3)!important;font-weight:500}
  /* 库存层：与上一行**同一网格**（列沿对齐），字号次一级 */
  .stat-rest{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0 20px}
  .stat-rest .stat{padding:2px 0}
  .stat-rest .stat + .stat{border-left:1px solid var(--line);padding-left:16px}
  .stat-rest .stat .k{font-size:13px;color:var(--ink-3);display:inline}
  .stat-rest .stat .v{font-size:20px;font-weight:600;margin-left:6px;
    font-variant-numeric:tabular-nums}
  .stat{line-height:1.4}
  .stat .k{color:var(--ink-3);font-size:13px}
  .stat .v{font-size:26px;font-weight:600;margin-top:2px;
    font-variant-numeric:tabular-nums}
  /* 库存层里 A/B/C/D 是档位分布，用四段细条比四个数字更快读出比例 */
  .tierbar{display:flex;height:4px;width:100%;gap:1px;margin-top:10px;border-radius:2px;overflow:hidden}
  .tierbar i{display:block;height:100%}
  .tierbar.is-empty{background:var(--line-2)}
  .tier-note{font-size:11px;color:var(--ink-3);margin-top:5px}
  /* v1.25.6：统计区改为"库存数字行 + 右侧投递进度环"；环段与图例都可点（穿透筛选） */
  .stat-row{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
  .tier-donut{display:flex;align-items:center;gap:14px;flex:none}
  .tier-donut svg .td-seg{cursor:pointer;transition:stroke-width .12s}
  .tier-donut svg .td-seg:hover{stroke-width:14}
  .tabs{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 12px}
  .tab{padding:7px 15px;border:1px solid var(--line);border-radius:var(--r-ctl);background:var(--surface);cursor:pointer;font-size:14px;color:var(--ink-2)}
  .tab:hover{color:var(--accent);border-color:var(--accent-line)}
  .tab.on{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-ink);font-weight:500}
  .chip{font-size:13px;padding:2px 9px;border-radius:var(--r-card);display:inline-block;margin:0 4px 4px 0}
  /* 档位徽章：只把字母加粗放大一点，说明文字缩小一号。
     注意这里刻意**克制**——卡片左侧已经有一条按档位着色的色条在表达"他是什么档"，
     徽章如果再做成大字号竖排就是第三次重复表达同一件事了。色条负责"扫一眼就知道"，
     徽章负责"停下来读清是哪一档"，两者分工，不重叠。 */
  .badge{font-size:13px;padding:3px 10px;border-radius:var(--r-ctl);font-weight:500;
    display:inline-flex;align-items:baseline;gap:5px;flex:0 0 auto;line-height:1.4}
  .badge b{font-size:16px;font-weight:600;letter-spacing:-.01em}
  .badge span{font-size:12px;font-weight:400;opacity:.85;white-space:nowrap}
  /* 不是档位徽章的地方（如系统页的模型名）用 .plain 退回单行文字样式 */
  .badge.plain{display:inline-block;font-size:13px;font-weight:500;padding:3px 10px;line-height:1.5}
  /* chip 两态：已核验技能 / 未核验技能。上一轮漏插了这段，导致这两个类名没有样式 */
  /* 投递管道的一行式条目（v1.15）：密度优先，字号与行高都比正文小一号 */
  .pipe-it{display:flex;align-items:center;gap:6px;line-height:1.55;font-size:12px;
    padding:3px 0;border-bottom:1px solid var(--surface-2)}
  .pipe-it:last-of-type{border-bottom:0}
  .pipe-nm{cursor:pointer;color:var(--accent-ink);white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;max-width:8em}
  .pipe-job{color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
  .pipe-day{color:var(--ink-3);flex:none;font-variant-numeric:tabular-nums}
  .pipe-day.is-over{color:var(--bad);font-weight:600}
  .pipe-sel{flex:none;padding:1px 2px;font-size:11px;border-radius:3px;max-width:5.5em}
  .pipe-more{font-size:11px;color:var(--ink-3);padding-top:4px}
  /* 归档年份卡片（v1.20）：比数字输入框友好，也不会出现负数/乱值 */
  .arch-cards{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
  .arch-card{border:1px solid var(--line);border-radius:var(--r-ctl);padding:6px 12px;
    cursor:pointer;line-height:1.35;background:var(--surface);min-width:104px}
  .arch-card b{display:block;font-size:14px}
  .arch-card span{font-size:11px;color:var(--ink-3)}
  .arch-card:hover{border-color:var(--accent);background:var(--accent-soft)}
  .arch-card.on{border-color:var(--accent);background:var(--accent-soft)}
  .arch-card.on b{color:var(--accent-ink)}
  /* 字典标签 + × 删除（v1.21）：让"删除"有明确入口 */
  /* 多值输入（v1.23）：标签 + 输入框；点 × 移除 */
  .multi{border:1px solid var(--line);border-radius:var(--r-ctl);padding:4px 6px;
    display:flex;flex-wrap:wrap;gap:5px;align-items:center;background:var(--surface)}
  .multi input:not([type=hidden]){border:0;outline:0;flex:1;min-width:160px;padding:3px 2px}
  .multi-chip{display:inline-flex;align-items:center;gap:4px;font-size:12px;
    padding:2px 5px 2px 8px;border:1px solid var(--line);border-radius:var(--r-chip);
    background:var(--accent-soft);color:var(--accent-ink)}
  .multi-chip b{cursor:pointer;font-weight:600;padding:0 2px;opacity:.65}
  .multi-chip b:hover{opacity:1;color:var(--bad)}
  .dict-tags{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:2px}
  .dict-tag{display:inline-flex;align-items:center;gap:4px;font-size:12px;
    padding:2px 6px 2px 8px;border:1px solid var(--line);border-radius:var(--r-chip);
    background:var(--surface-2);color:var(--ink-2)}
  .dict-tag b{cursor:pointer;color:var(--ink-3);font-weight:600;padding:0 2px}
  .dict-tag b:hover{color:var(--bad)}
  /* 批量归档分组条（v1.22.1）：把散落的控件收成一组，
     用细线分隔"勾选的人"与"按年"两种方式，不再是四个孤立控件 */
  .arch-bar{display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;
    background:var(--surface-2);border:1px solid var(--line);
    border-radius:var(--r-ctl);padding:6px 10px}
  .arch-bar .arch-label{font-size:12px;color:var(--ink-3);font-weight:600;
    letter-spacing:.02em}
  .arch-bar select{min-width:96px}
  .arch-bar .arch-suffix{font-size:13px;color:var(--ink-2);margin-left:-4px}
  .arch-bar .arch-div{width:1px;height:20px;background:var(--line);
    margin:0 2px}
  /* 字典逐项列表（v1.21.2）：每项一行 + × 删除 + 输入框添加 */
  .dict-list{border:1px solid var(--line);border-radius:var(--r-ctl);
    max-height:190px;overflow:auto;background:var(--surface)}
  .dict-row{display:flex;align-items:center;gap:8px;padding:4px 9px;font-size:13px}
  .dict-row:nth-child(odd){background:var(--surface-2)}
  .dict-row span{flex:1;min-width:0}
  .dict-row b{cursor:pointer;color:var(--ink-3);font-weight:600;padding:0 4px;flex:none}
  .dict-row b:hover{color:var(--bad)}
  .dict-add{display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap}
  .dict-add input{flex:1;min-width:120px}
  .chip-skill{color:var(--ink-2);background:var(--surface-2);border:1px solid var(--line)}
  .chip-unverified{color:var(--ink-3);background:transparent;border:1px dashed var(--line-2)}
  /* 投递管道的一行式条目（v1.15）：密度优先，字号与行高都比正文小一号 */
  .pipe-it{display:flex;align-items:center;gap:6px;line-height:1.55;font-size:12px;
    padding:3px 0;border-bottom:1px solid var(--surface-2)}
  .pipe-it:last-of-type{border-bottom:0}
  .pipe-nm{cursor:pointer;color:var(--accent-ink);white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;max-width:8em}
  .pipe-job{color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
  .pipe-day{color:var(--ink-3);flex:none;font-variant-numeric:tabular-nums}
  .pipe-day.is-over{color:var(--bad);font-weight:600}
  .pipe-sel{flex:none;padding:1px 2px;font-size:11px;border-radius:3px;max-width:5.5em}
  .pipe-more{font-size:11px;color:var(--ink-3);padding-top:4px}
  .row1{display:flex;align-items:center;gap:14px;line-height:1.5}
  /* 头像：中性灰，不参与档位编码。
     原来头像底色= 档位色，和左侧色条、右上徽章**同时**表达同一件事，
     一张卡上三个地方说"他是 A 档"。现在只留色条+ 徽章说档位，
     头像回归它的本职——让人名首字好找。 */
  .avatar{width:44px;height:44px;border-radius:50%;display:flex;align-items:center;
    justify-content:center;font-size:17px;font-weight:600;flex:0 0 auto;
    color:var(--ink-2);background:var(--surface-2)}
  .nm{font-size:18px;font-weight:600;letter-spacing:-.01em}
  .meta{font-size:13px;color:var(--ink-3);margin-top:3px}
  .acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px;line-height:1.5}
  .why{font-size:14px;color:var(--ink-2);margin-top:10px;line-height:1.75}
  .ev{font-size:13px;color:var(--ink-3);background:var(--surface-2);border-left:2px solid var(--line-2);
    padding:5px 9px;margin:4px 0;border-radius:0 4px 4px 0}
  .modal{position:fixed;inset:0;background:rgba(29,33,41,.45);display:none;align-items:center;
    justify-content:center;padding:22px;z-index:20}
  .modal.on{display:flex}
  .sheet{background:var(--surface);border-radius:var(--r-card);max-width:920px;width:100%;max-height:calc(88vh / var(--z));
    overflow:auto;padding:22px 24px}
  .progtrack{height:10px;background:var(--surface-2);border-radius:6px;overflow:hidden;margin:12px 0 8px}
  .progfill{height:100%;background:var(--accent);border-radius:6px;transition:width .3s ease}
  /* v1.24.20：不确定时长的等待态。总耗时未知时**不要假装有百分比**
     ——写死35% 会被当成"卡在35%"（HR 的原话）。循环摆动只表达"还在动"。 */
  .progfill--indet{width:35%;animation:progindet 1.4s ease-in-out infinite}
  @keyframes progindet{0%{transform:translateX(-100%);margin-left:0}
                       50%{margin-left:30%}
                       100%{transform:translateX(280%);margin-left:0}}
  .progmeta{font-size:13px;color:var(--ink-2);line-height:1.6}
  /* 居中提示层（v1.24.9）：固定在视口中央偏上，pointer-events:none 让它不挡操作，
     每条提示自己 pointer-events:auto 所以能点、能关。多条时纵向堆叠不重叠。 */
  .poplayer{position:fixed;left:50%;top:38%;transform:translate(-50%,-50%);z-index:60;
    display:flex;flex-direction:column;gap:10px;align-items:center;pointer-events:none;
    max-width:min(560px,92vw)}
  /* v1.24.11：不再整块可点（会让人以为要点它才能关），改成"文案 + 右上角 ×"。
     cursor 恢复默认，视觉上明确只有 × 是可点关闭的。 */
  .poplayer .pop{pointer-events:auto;min-width:260px;max-width:100%;
    display:flex;align-items:flex-start;gap:10px;padding:10px 10px 10px 14px;
    border-radius:var(--r-ctl);font-size:14px;
    box-shadow:0 8px 28px rgba(29,33,41,.18);
    animation:popIn .18s ease-out}
  .poplayer .pop-body{flex:1;min-width:0;line-height:1.6;word-break:break-word}
  .poplayer .pop-x{flex:none;width:22px;height:22px;line-height:1;padding:0;
    border:1px solid rgba(0,0,0,.10);border-radius:6px;background:rgba(255,255,255,.75);
    color:inherit;font-size:15px;cursor:pointer;opacity:.7}
  .poplayer .pop-x:hover{opacity:1;background:var(--surface)}
  /* 页面其它位置被点时提示全消失（JS 侧实现），提示本身淡一下给反馈 */
  @keyframes popIn{from{opacity:0;transform:translateY(-8px) scale(.98)}
                    to{opacity:1;transform:none}}
  .poplayer .pop.info{background:var(--accent-soft);border:1px solid var(--accent-line);color:var(--accent-ink)}
  .poplayer .pop.ok{background:var(--ok-soft);border:1px solid var(--ok-line);color:var(--ok)}
  .poplayer .pop.warn{background:var(--warn-soft);border:1px solid var(--warn-line);color:var(--warn)}
  .poplayer .pop.danger{background:var(--bad-soft);border:1px solid var(--bad-line);color:var(--bad)}
  /* 收件人邮箱展示块（v1.24.9）：字号加大 + 描边 + 等宽字体。
     为什么这么强调：发信不可撤销，收件人地址错一位就是发错人。
     HR 需要在"生成草稿 → 确认发送"之间一眼核对，不该是灰色小字。 */
  .mailto{display:flex;align-items:center;gap:10px;padding:10px 13px;max-width:100%;
    background:var(--ok-soft);border:1px solid var(--ok-line);border-left:3px solid var(--ok);
    border-radius:var(--r-ctl)}
  .mailto .ml{font-size:12px;color:var(--ink-2);flex:none}
  .mailto .mv{font-size:15px;font-weight:600;color:var(--ok);
    font-family:ui-monospace,Consolas,monospace;word-break:break-all}
  .mailto .mw{font-size:15px;font-weight:600;color:var(--warn)}
  .cand-drop{position:absolute;z-index:30;background:var(--surface);border:1px solid var(--line);
    border-radius:var(--r-ctl);max-height:240px;overflow:auto;box-shadow:0 6px 20px rgba(29,33,41,.12)}
  .cand-drop .cand-item{padding:8px 12px;cursor:pointer;font-size:14px;border-bottom:1px solid var(--surface-2)}
  .cand-drop .cand-item:hover,.cand-drop .cand-item.hi{background:var(--accent-soft)}
  .cand-drop .cand-item .em{color:var(--ink-2);font-size:12px}
  pre{white-space:pre-wrap;word-break:break-word;font-size:14px;color:var(--ink-2);line-height:1.8;
    background:var(--surface-2);padding:14px;border-radius:var(--r-ctl);margin:0}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--surface-2);vertical-align:top}
  th{color:var(--ink-3);font-weight:500;background:var(--surface-2)}
  .note{color:var(--ink-3);font-size:13px;line-height:1.75}
  .warn{background:var(--warn-soft);border:1px solid var(--warn-line);color:var(--warn);border-radius:var(--r-ctl);padding:10px 13px;font-size:14px}
  .info{background:var(--accent-soft);border:1px solid var(--accent-line);color:var(--accent-ink);border-radius:var(--r-ctl);padding:10px 13px;font-size:14px}
  .danger{background:var(--bad-soft);border:1px solid var(--bad-line);color:var(--bad);border-radius:var(--r-ctl);padding:10px 13px;font-size:14px}
  .ok{background:var(--ok-soft);border:1px solid var(--ok-line);color:var(--ok);border-radius:var(--r-ctl);padding:10px 13px;font-size:14px}
  /* v1.24.20：对话区撑满可视高度、且**页面不出现多余下滑**。
   原来是 max-height:440px 固定矮框 —— 页面下面留一大片空白，
   对话气泡挤在小窗口里上下滚动（用户原话："把智能助手这个对话框扩大到页面大小"）。

   ⚠️ 踩过的坑：第一版写的是 min-height:calc(100vh - 190px)，
   **页面底部多出约 40px 的下滑条**（用户反馈"页面下面好像有一个可以下滑的区域"）。
   成因：.main 有 padding-bottom:60px、.layout 是 min-height:100vh，
   用 100vh 算出来的容器比可视区高出整整一个底padding —— 它是**内容**溢出，
   不是容器溢出，所以改overflow 也治不了。

   正确做法：**不自己算 100vh**，改成让布局自己分配高度。
   .layout 换成 height:100vh + overflow:hidden，
   .main 内部用 flex 列，.chat-page 靠 flex:1 自然填满剩余空间。
   这样窗口尺寸怎么变都对，不存在需要我算的常量。

   ⚠️ 第二个坑：这组覆盖规则最初写在**这里**（.chatlog 附近，第 390 行），
   而原始的 .layout/.main/.main-inner 定义在文件更前的第 109~128 行。
   CSS 同优先级**按出现顺序**生效，后写的赢—— 于是原始的 min-height:100vh
   把height:100vh 盖掉了，overflow:hidden 也被后面的默认 overflow 盖掉，
   结果完全没生效（截图：输入框被顶到视口外，右侧多一条滚动条）。
   **覆盖同一选择器必须写在原始定义之后**，或者直接改原始那一行。
   —— 这个坑和「后加的 CSS 不起作用」是同一类，值得单独记。 */
  .chat-page{display:flex;flex-direction:column;flex:1;min-height:0}
  /* flex:1 + min-height:0 是关键：不给 min-height:0，
     内容会把flex 容器顶高，溢出又被推回页面滚动条上。 */
  .chatlog{margin-top:12px;overflow:auto;display:flex;flex-direction:column;gap:9px;
    flex:1;min-height:0}
  .chat-foot{margin-top:10px;flex:0 0 auto}
  .msg{font-size:15px;line-height:1.75;padding:10px 13px;border-radius:var(--r-card);white-space:pre-wrap}
  .msg.user{background:var(--accent-soft);align-self:flex-end;max-width:78%}
  .msg.assistant{background:var(--surface-2);max-width:94%}
  .msg.md{white-space:normal;word-break:break-word}
  .msg.md p{margin:0 0 8px}.msg.md p:last-child{margin-bottom:0}
  .msg.md h1,.msg.md h2,.msg.md h3,.msg.md h4{font-size:15px;font-weight:700;margin:10px 0 6px}
  .msg.md ul,.msg.md ol{margin:4px 0 8px;padding-left:20px}
  .msg.md li{margin:2px 0}
  .msg.md code{background:var(--surface);border:1px solid var(--line);border-radius:4px;
    padding:1px 5px;font-size:13px;font-family:ui-monospace,Consolas,monospace}
  .msg.md pre{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-ctl);
    padding:10px 12px;overflow:auto;margin:6px 0 10px}
  .msg.md pre code{background:none;border:none;padding:0}
  .msg.md strong{font-weight:700}
  .msg.md a{color:var(--accent);text-decoration:underline}
  /* v1.24.20：Markdown 表格。模型做"全员对比"时一定出表格，
     列数常6~9 个，窄屏必须能横向滚——直接铺开会撑破气泡布局。
     描边只用 --line 且只画横线（分隔线优先于卡片堆叠，同全局原则），
     表头一档底色区分，不引入第二个强调色。 */
  .mdtable-wrap{overflow-x:auto;margin:8px 0 10px;-webkit-overflow-scrolling:touch}
  .mdtable{border-collapse:collapse;font-size:13px;line-height:1.6;min-width:100%}
  .mdtable th,.mdtable td{border-bottom:1px solid var(--line);padding:6px 10px;
    text-align:left;vertical-align:top;white-space:normal}
  .mdtable th{background:var(--surface);font-weight:700;white-space:nowrap}
  .mdtable tbody tr:last-child td{border-bottom:none}
  .mdtable tbody tr:hover td{background:var(--surface)}
  .trace{font-size:13px;color:var(--ink-3);margin-top:8px;border-top:1px dashed var(--line);padding-top:8px}
  /* v1.25：工具轨迹折叠盒。默认收起，只在 HR 想看"你到底做了什么"时展开。 */
  .tracebox{margin-top:10px;font-size:13px}
  .tracebox > summary{cursor:pointer;color:var(--ink-3);user-select:none;padding:2px 0}
  .tracebox > summary:hover{color:var(--ink-2)}
  .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
  .pcol{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-card);padding:12px 14px}
  .pcol .h{display:flex;justify-content:space-between;font-size:14px;color:var(--ink-2);font-weight:600}
  .pcol .it{font-size:13px;color:var(--ink-3);margin-top:6px;border-top:1px dashed var(--surface-2);padding-top:6px}
  .flexbetween{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
  .small{font-size:12px;color:var(--ink-3)}
  .kv{display:grid;grid-template-columns:140px 1fr;gap:6px 12px;font-size:14px}
  .kv .k{color:var(--ink-3)}
  .spacer{height:10px}
  .job-tag{color:var(--accent-ink);background:var(--accent-soft)}
  .job-tag.pending{color:var(--warn);background:var(--warn-soft)}
  .contact{font-size:14px}
  .contact a{color:var(--accent-ink);text-decoration:none;border-bottom:1px dashed var(--accent-line)}
  .contact a:hover{color:var(--accent-ink);border-bottom-style:solid}
  button.mini{font-size:12px;padding:1px 7px;margin-left:6px;border-radius:var(--r-ctl)}
  .srcbox{background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-ctl);padding:10px 12px;font-size:14px}
  .srcbox code{background:var(--surface-2);padding:1px 6px;border-radius:5px;font-size:13px}
  .jdform{display:grid;grid-template-columns:150px 1fr;gap:8px 12px;align-items:center;margin-top:6px}
  .jdform label{color:var(--ink-2);font-size:14px}
  .jdform input,.jdform textarea,.jdform select{width:100%}
  /* 岗位表的 JD 摘要可能很长，限宽后把「投递数/状态/操作」挤窄了会换行，
     所以 JD 列限宽 + 其余列禁止折行 */
  .jdsum{max-width:430px;line-height:1.6}
  .nw{white-space:nowrap}
  .res-cell{font-size:13px;line-height:1.5}
  /* 邮件正文：**所见即所得**编辑器（白底、贴近收件人看到的样式）。
     表格样式必须写在这里：编辑器里看到的边框/内边距，就是收件人看到的样子
     （发出去的 HTML 也带同样的内联样式，见 mail_template.to_html 与编辑器产物）。 */
  .richeditor{border:1px solid var(--line);border-radius:var(--r-ctl);padding:12px;background:var(--surface);
    min-height:240px;max-height:460px;overflow:auto;line-height:1.75;font-size:14px;
    font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif}
  .richeditor:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
  .richeditor table{border-collapse:collapse;margin:8px 0}
  .richeditor th,.richeditor td{border:1px solid var(--line-2);padding:6px 10px;min-width:64px}
  .richeditor th{background:var(--surface-2);font-weight:600}
  .ok-txt{color:var(--ok)}.warn-txt{color:var(--warn)}.bad-txt{color:var(--bad)}
</style></head>
<body>
<div class="layout">
  <aside class="side">
    <div class="brand">__BRAND_LOGO__<span>企业人才库智能体</span></div>
    <nav class="nav" id="tabs"></nav>
    <div class="sidefoot" id="sideUser"></div>
  </aside>
  <main class="main"><div class="main-inner">
    <div class="grid-stats" id="stats"></div>
    <div id="modelWarn"></div>
    <div id="alerts"></div>
    <div id="view"></div>
  </div></main>
</div>

<div class="modal" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="sheet"><div class="flexbetween" style="margin-bottom:14px">
    <div style="font-size:18px;font-weight:600" id="mTitle">详情</div>
    <div class="bar"><span id="mHeadBtn"></span><button onclick="closeModal()">关闭</button></div></div>
    <div id="mBody"></div></div>
</div>

<!-- 居中提示层（v1.24.9）：所有 toast 提示都落在这里，不再往页面顶部塞。
     为什么必须居中：#alerts 在 .main-inner 顶部，而实际操作多在页面下方
     （收件人、发信按钮、批量操作都在下面）——提示在顶部等于没有提示。 -->
<div class="poplayer" id="popLayer"></div>

<script>
const AUTH_ENABLED = __AUTH_ENABLED__;
// 前端版本戳（与 <meta name="tp-build">、/api/meta 的 ui_build 同源）：
// 验收脚本用它确认"页面里跑的就是服务端当前这版"，防止静默地验了缓存里的旧代码。
const UI_BUILD = '__UI_BUILD__';
const TIER_LABELS = {A:'优先面试',B:'建议面试',C:'储备',D:'暂不匹配当前岗位'};
const TIER_COLOR = {A:['var(--ok)','var(--ok-soft)'],B:['var(--tier-b)','var(--tier-b-soft)'],
                    C:['var(--warn)','var(--warn-soft)'],D:['var(--ink-3)','var(--surface-2)']};
const STAGES = ['新投递','已联系','初面','复面','待offer','已入职','已结束'];

let META = null;
// 归档页当前列表（批量彻底删除时按id 取姓名做确认文案）
let ARCH_ITEMS = [];
/** 搜索模式：kw=关键词（字面）｜sem=语义（按意思）。
 *  v1.14：语义检索的函数一直存在但界面上没有入口（孤儿函数），HR 反馈"没有语义检索入口"，
 *  这里把它接回同一个搜索栏。语义走的是向量检索，**只用于找人，不参与档位判定**。 */
let SEARCH_MODE = 'kw';
let _semDegraded = false;   // 语义检索是否正在降级（提示只在真正用到时才出现）
let TOKEN = localStorage.getItem('tp_token') || '';
let VIEW = 'pool', TAB = 'ALL', KW = '', CHAT = [], ITEMS = [], CUR = null, LAST_INGEST = null;
// 校招场景（HR 口径）：**一律按应届处理，不推断也不展示工作年限**。
// 刻意不做成开关——「能不能投」看的是身份，不是一个年限数字；
// 留一个可切换的开关反而会让人以为「打开年限分析更准」。
const CAMPUS_MODE = true;
// 性别筛选：**默认不筛**（空串）。开关在设置里默认关闭，关着时后端也会忽略这个参数。
let GENDER = '';
// 初筛下拉（v1.7.3）：最低学历（≥门槛）与院校层次（985/211）。
// 都是岗位相关的硬条件，与性别筛选不同，不需要开关约束。
let EDU_MIN = '', UNIV = '';
let STAGE_F = '';          // v1.16.1：阶段筛选（管道"看全部"跳过来时用）
const PIPE_ALL = {};      // 管道里手动展开的列：{阶段名: true}
// 人才库分页（v1.7.1）：每页 10 人。切档位 / 搜索 / 清空都会把页码拨回第 1 页——
// 否则"在第 3 页改了搜索词"会落在一个不存在的页上（后端会兜底夹到末页，但那不是用户想要的）。
// 导出 CSV 不分页：另发一次不带 page 参数的请求拿全量，见 exportCsv。
let POOL_PAGE = 1;const POOL_SIZE = 10;
// 投递管道看板折叠态（v1.7.5）：默认收起只看一行人数，展开才是完整看板；
// 记在全局变量上，翻页 / 搜索 / 改档位等 refresh 重渲染后不丢。
let PIPE_OPEN = false;
function poolPageReset(){ POOL_PAGE = 1; }

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
/* 轻量 Markdown 渲染（v1.24.9）：智能助手回复是 Markdown，直接当纯文本会
   裸奔出 ** / # / - / ` 等符号。先把原文转义（防 XSS），再转换常见标记。
   支持：代码块、行内代码、加粗、#~#### 标题、-/* 无序列表、1. 有序列表、链接、换行。 */
function md(src){
  let s = String(src==null?'':src);
  // 1) 先转义一切 HTML，杜绝注入
  s = esc(s);
  // 2) 代码块（``` 或 ~~~），先占位，避免里面的标记被后续替换
  //    v1.24.15 修：① 去掉首行语言标记（```python 里的 python 不该显示）
  //    ② 去掉代码块紧邻行首尾的空行，否则 <pre> 前后多出空行
  //    ③ 占位符自带块标记，让行组装阶段把它当独立块，而不是塞进 <p>
  const blocks = [];
  s = s.replace(/```[ \t]*([A-Za-z0-9+#._-]*)[ \t]*\\n?([\\s\\S]*?)```/g, (m, lang, code) => {
    blocks.push('<pre><code>' + code.replace(/^\\n+|\\n+$/g, '') + '</code></pre>');
    return '\\n<pre>\\u0000BLOCK' + (blocks.length - 1) + '\\u0000</pre>\\n';
  });
  // 3) 行内代码
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  // 4) 加粗
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // 5) 标题（行首 #）
  s = s.replace(/^######[ \t]+(.+)$/gm, '<h4>$1</h4>');
  s = s.replace(/^#####[ \t]+(.+)$/gm, '<h4>$1</h4>');
  s = s.replace(/^####[ \t]+(.+)$/gm, '<h4>$1</h4>');
  s = s.replace(/^###[ \t]+(.+)$/gm, '<h3>$1</h3>');
  s = s.replace(/^##[ \t]+(.+)$/gm, '<h3>$1</h3>');
  s = s.replace(/^#[ \t]+(.+)$/gm, '<h2>$1</h2>');
  // 6) 链接 [文字](url)
  //    v1.24.15 修：原来只认 https?://，而模型很爱写站内锚点（[投递管道](#pool)），
  //    这些会原样露出 markdown 符号。这里放开到任意协议与相对路径。
  //    安全：href 里的引号已在第 1 步被转义，javascript: 再由下面的白名单挡掉。
  s = s.replace(/\[([^\]]+)\]\(([^)\\s]+)\)/g, (m, txt, url) => {
    const u = String(url);
    if (!/^(https?:\\/\\/|mailto:|\\/|#|\\.)/i.test(u)) return m;  // 未知协议当纯文本
    return '<a href="' + u + '" target="_blank" rel="noopener">' + txt + '</a>';
  });
  // 7) 逐行组装：列表项聚合成 <ul>，空行分段，其余行转 <p>
  // 注意：源码里必须写 \\n/\\r（双反斜杠），Python 三引号会把单反斜杠转义成真实换行
  const lines = s.split(/[\\n\\r]+/);
  const out = [];
  let liBuf = [], liTag = 'ul';   // v1.24.15：记住当前列表是 ul 还是 ol
  const flushLi = () => {
    if (liBuf.length){
      // 两种列表不能塞进同一个 <ul>，否则序号会串
      out.push('<' + liTag + '>' + liBuf.join('') + '</' + liTag + '>');
      liBuf = [];
    }
  };
  let paraBuf = [];
  const flushPara = () => {
    if (paraBuf.length){ out.push('<p>' + paraBuf.join('<br>') + '</p>'); paraBuf = []; }
  };
  // ---- v1.24.20：Markdown 表格 ----
  // 模型做"全员对比"时几乎一定输出管道表格，而 md() 原先只认
  // 代码块/加粗/标题/列表/链接，表格整块以原始文本裸奔
  // （用户原话："回复页面还是没有实现表格的正常渲染"）
  // —— `| :--- |` 这些符号直接怼在页面上。
  //
  // 判据：**表头行 + 分隔行**同时存在才算表格。
  //   光看"有竖线"会误伤正文里的 "A | B" 这种写法；
  //   而分隔行（`:---`、`---:`、`---`）才是表格的确证。
  //
  // 放在行组装这一步做：表格是**连续多行**的结构，
  // 在此之前已单独处理掉代码块/行内代码/加粗/链接，行内不会被污染。
  const isSepRow = t => /^\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?$/.test(t);
  const splitRow = t => t.replace(/^\||\|$/g, '').split('|').map(c => c.trim());
  // 单元格里的 <br> 是模型故意换行（"命中: 材料成型,<br>缺失: 无"），
  // esc() 已经把原文里的尖括号转义过，这里要还原成真换行。
  const cell = c => c.replace(/&lt;br\s*\/?&gt;/gi, '<br>');
  for (let i = 0; i < lines.length; i++){
    const t = lines[i].trim();
    // 表头：含竖线，且下一行是分隔行
    if (t.includes('|') && i + 1 < lines.length && isSepRow(lines[i + 1].trim())){
      flushLi(); flushPara();
      const head = splitRow(t);
      i += 1;                       // 跳过分隔行
      const body = [];
      while (i + 1 < lines.length){
        const nxt = lines[i + 1].trim();
        if (!nxt || !nxt.includes('|')) break;
        body.push(splitRow(nxt));
        i += 1;
      }
      const cols = head.length;
      // 列数对齐：缺列补空、多列截断。模型偶尔会写歪，截断会串位。
      const fit = r => Array.from({length: cols}, (_, k) => cell(r[k] == null ? '' : r[k]));
      let html = '<div class="mdtable-wrap"><table class="mdtable"><thead><tr>'
        + head.map(h => '<th>' + cell(h) + '</th>').join('')
        + '</tr></thead><tbody>';
      body.forEach(r => { html += '<tr>' + fit(r).map(c => '<td>' + c + '</td>').join('') + '</tr>'; });
      html += '</tbody></table></div>';
      out.push(html);
      continue;
    }
    if (!t){ flushLi(); flushPara(); continue; }
    if (/^[-*+][ \t]+/.test(t)){
      flushPara();
      if (liBuf.length && liTag !== 'ul') flushLi();   // 换类型前先收尾
      liTag = 'ul';
      liBuf.push('<li>' + t.replace(/^[-*+][ \t]+/, '') + '</li>');
      continue;
    }
    if (/^\d+\.[ \t]+/.test(t)){
      flushPara();
      if (liBuf.length && liTag !== 'ol') flushLi();
      liTag = 'ol';
      liBuf.push('<li>' + t.replace(/^\d+\.[ \t]+/, '') + '</li>');
      continue;
    }
    if (/^<\\/?(ul|ol|pre|h[1-4])\\b/.test(t)){ flushLi(); flushPara(); out.push(t); continue; }
    flushLi();
    paraBuf.push(t);
  }
  flushLi(); flushPara();
  s = out.join('');
  // 8) 还原代码块占位
  //    v1.24.15 修：<pre> 是块级元素，不能待在 <p> 里（浏览器会拆断，样式错乱）。
  //    还原时前后补换行，让行组装阶段把它当独立块；并把紧贴着的多余 <br> 收掉。
  s = s.replace(/<pre>\\s*\\u0000BLOCK(\\d+)\\u0000\\s*<\\/pre>/g, (m, i) => blocks[+i]);
  s = s.replace(/\\u0000BLOCK(\\d+)\\u0000/g, (m, i) => blocks[+i]);
  return s;
}
function hdr(extra){
  const h = Object.assign({'Content-Type':'application/json'}, extra||{});
  if (AUTH_ENABLED && TOKEN) h['X-TP-Token'] = TOKEN;
  return h;
}
async function api(path, opts){
  opts = opts || {};
  opts.headers = hdr(opts.headers);
  const r = await fetch(path, opts);
  let body = null;
  const raw = await r.text();               // 先取文本：失败时要能看见证据
  try { body = JSON.parse(raw); } catch(e) {
    // v1.23.7：**别只报「响应不是 JSON」**。服务端 500 时返回的是 HTML 报错页，
    // 把真实异常盖成一句无信息量的提示——实测害我多排查一轮
    // （真实原因是一个 NameError，只在服务端日志里，界面上完全看不到）。
    // 现在把状态码与正文片断带出来，错误当场可见。
    body = {error: `服务端返回了非 JSON 响应（HTTP ${r.status}）`,
            detail: (raw || '').slice(-400) || '（空响应）',
            __non_json: true};
  }
  if (!r.ok) return Object.assign({__http_error:r.status}, body||{});
  return body;
}
/* 全局提示（v1.24.9 改：顶部内联 → 居中弹窗；v1.24.11 再改交互）
   原来往#alerts（.main-inner 顶部）prepend 一个 div，操作在页面下方时根本看不见。
   现在统一落到视口中央的 #popLayer。
   v1.24.11 按试用反馈重做交互（原来是"只能点提示本身才关 + 自动停 6/9 秒"）：
     1) 每条提示右上角都有显式的 ×，不用猜哪儿能点；
     2) 点页面其他任何位置，当前所有提示立即消失（点提示内部不关，避免误操作）；
     3) 不再长时间赖着不走——自动消失压到 3.5/5 秒。
   132 个调用点不用逐个改——只改这个函数，全站提示一次性生效。 */
function toast(msg, kind){
  const host = document.getElementById('popLayer');
  if (!host) return;
  const d = document.createElement('div');
  d.className = 'pop ' + (kind || 'info');
  // msg 允许带 **粗体**（有几处调用就是这么传的），转成 <br>/<b> 再显示。
  // 走 textContent 之外的路径前先escape，杜绝注入。
  const escd = String(msg == null ? '' : msg)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/\\n/g, '<br>');
  // 结构：文案区 + 独立关闭按钮。按钮自带 stopPropagation，
  // 这样"点提示内部不关闭"和"× 能关闭"两条规则不会互相打架。
  const box = document.createElement('div');
  box.className = 'pop-body';
  box.innerHTML = escd;
  const x = document.createElement('button');
  x.className = 'pop-x';
  x.type = 'button';
  x.title = '关闭';
  x.setAttribute('aria-label', '关闭');
  x.innerHTML = '&times;';
  x.onclick = (ev) => { ev.stopPropagation(); d.remove(); };
  d.appendChild(box);
  d.appendChild(x);
  host.appendChild(d);
  // 自动消失时间：成功/警告稍长（发信不可撤销，闪一下等于没提示），但都压到几秒内
  const ms = (kind === 'ok' || kind === 'warn' || kind === 'danger') ? 5000 : 3500;
  setTimeout(() => d.remove(), ms);
  // 同类提示最多留 4 条，避免批量操作时刷屏把屏占满
  const same = [...host.children].filter(y => y.className.includes(kind || 'info'));
  while (same.length > 4) same.shift().remove();
}

/* 点页面其他位置 → 所有居中提示立即消失。
   capture=true 抢在业务 onclick 之前跑，保证"点哪儿都能关"。
   只认落在提示层之外的点击；点在提示、详情弹窗、收件人下拉里一律不关。 */
document.addEventListener('click', (ev) => {
  const host = document.getElementById('popLayer');
  if (!host || !host.children.length) return;
  const t = ev.target;
  if (host.contains(t)) return;
  if (t.closest && t.closest('#modal')) return;
  if (t.closest && t.closest('.cand-drop')) return;
  host.innerHTML = '';
}, true);

/* 居中结果弹窗（v1.24.9）：导入/分析这类耗时操作的**成败**要让人一眼看到。
   不再用顶部 toast —— 操作在页面下方、提示在顶部，等于没提示。
   opts: {title, body(html), kind: 'ok'|'danger'|'warn', okText} */
/* opts.afterClose：关闭时再执行的动作。
   为什么需要：入库流程里有两步（先"收取完成"，再"自动分析进度"），
   而 resultModal 和 progressModal 共用同一个 #modal —— 第二个弹窗会瞬间覆盖第一个。
   v1.24.11 之前用户实际只看到最后的进度条，"收取完成"的成交通知被吃掉了。
   现在改成：先让用户看完收取结果并点"知道了"，再接着开分析进度窗。 */
let __afterClose = null;
function resultModal(opts){
  const o = opts || {};
  __afterClose = o.afterClose || null;
  const kind = o.kind || 'info';
  const color = kind==='ok' ? 'var(--ok)' : (kind==='danger' ? 'var(--bad)' : (kind==='warn' ? 'var(--warn)' : 'var(--accent)'));
  setModalTitle(o.title || '完成');
  document.getElementById('mBody').innerHTML =
    `<div style="line-height:1.8;white-space:pre-wrap">
       <div style="font-size:15px;color:${color};font-weight:600;margin-bottom:8px">${o.headline || ''}</div>
       <div>${o.body || ''}</div>
     </div>
     <div class="bar" style="margin-top:18px;justify-content:flex-end">
       <button class="btn-primary" onclick="closeModal()">${esc(o.okText || '知道了')}</button>
     </div>`;
  document.getElementById('modal').classList.add('on');
}

/* 居中进度弹窗（v1.24.9）：带真实进度条。
   taskId 来自后端的任务登记；轮询 /api/tasks/{taskId}，每 1.2 秒刷新进度。
   完成后自动把弹窗换成 resultModal（成功/失败），并回调 onDone(taskResult)。 */
let __progressTimer = null;
function progressModal(taskId, opts){
  const o = opts || {};
  if (!taskId){ resultModal({title: o.title||'处理中', body:o.body||'', kind:'info'}); return; }
  if (__progressTimer){ clearInterval(__progressTimer); __progressTimer = null; }
  setModalTitle(o.title || '处理中…');
  document.getElementById('mBody').innerHTML =
    `<div>
       <div style="font-size:14px;color:var(--ink-2);margin-bottom:4px">${esc(o.desc || '正在处理，请稍候')}</div>
       <div class="progtrack"><div class="progfill" id="progFill" style="width:0%"></div></div>
       <div class="progmeta" id="progMeta">准备中…</div>
     </div>`;
  document.getElementById('modal').classList.add('on');
  const fill = document.getElementById('progFill');
  const meta = document.getElementById('progMeta');
  const tick = async () => {
    try {
      const r = await api('/api/tasks/' + taskId);
      if (r.__http_error || !r.ok){ return; }
      const pct = r.percent || 0;
      if (fill) fill.style.width = pct + '%';
      const line = `已完成 ${r.done || 0} / ${r.total || 0}` +
                   (r.failed ? `，失败 ${r.failed}` : '');
      if (meta){
        meta.textContent = r.status === 'done'
          ? (r.message || line)
          : (r.message ? (line + ' · ' + r.message) : line);
      }
      if (r.status === 'done' || r.status === 'error'){
        clearInterval(__progressTimer); __progressTimer = null;
        const okAll = !(r.failed);
        const title = r.status === 'error' ? '处理中断' : (okAll ? '全部完成' : '有失败');
        const kind = r.status === 'error' ? 'danger' : (okAll ? 'ok' : 'warn');
        resultModal({
          title, kind,
          headline: (r.status === 'error' ? '任务出错' : (okAll ? '处理成功' : '部分失败')),
          body: (r.message || '') +
                `<div style="margin-top:6px;color:var(--ink-2)">共 ${r.total} 人：完成 ${r.done} 人`
                + (r.failed ? `，失败 ${r.failed} 人` : '') + '</div>'
        });
        if (o.onDone) o.onDone(r);
      }
    } catch(e){ /* 网络抖动忽略，下一轮继续 */ }
  };
  tick();
  __progressTimer = setInterval(tick, 1200);
}

/* v1.25：复制草稿的**主题 + 正文**（v1.25 修）。
   原来的按钮写成 `copyText(JSON.stringify(''))` —— 复制的是空字符串，
   点了只弹一句"已复制"，剪贴板里其实什么都没有。 */
function copyDraft(key){
  const d = (DRAFTS || {})[key];
  if (!d){ toast('这封草稿已经不在了','warn'); return; }
  /* ⚠️ 下面拼接里的换行必须写成双反斜杠形式（见本文件顶部 3.2 那条）：
     _PAGE 是普通三引号字符串而非 raw string，单反斜杠的换行符会被 Python
     先转成**真实换行**，直接截断 JS 字符串 → 整页白屏。
     MEMORY.md 3.2 记过这个坑，本次又踩了一次（注释里写同样的转义也会中招）。 */
  const txt = (d.subject ? ('主题：' + d.subject + '\\n\\n') : '') + (d.body || '');
  copyText(txt);
}
function copyText(t){
  if (!t || t==='—'){ toast('没有可复制的内容','warn'); return; }
  navigator.clipboard.writeText(t).then(()=>toast('已复制：'+t,'ok'),
    ()=>{ toast('浏览器拒绝了复制，请手动选中复制','warn'); });
}
// 预览/下载走浏览器原生导航，带不了自定义请求头；登录模式下用 ?token= 兜底
function withToken(url){
  if (!(AUTH_ENABLED && TOKEN)) return url;
  return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
}
// 简历原件：inline=1 在浏览器里直接打开（PDF、文本可预览），否则下载
function openDoc(id, inline){
  const url = withToken('/api/documents/'+id+'/file'+(inline?'?inline=1':''));
  if (inline) window.open(url, '_blank');
  else location.href = url;
}
// 来源文件夹里的简历：还没入库也能先看/先下（HR 得先看内容再决定导不导入）
function openSourceFile(name, inline){
  const url = withToken('/api/sources/file?inline='+(inline?1:0)+'&name='+encodeURIComponent(name));
  if (inline) window.open(url, '_blank');
  else location.href = url;
}
function downloadSourceFile(name){ openSourceFile(name, 0); }
function zipNameFrom(resp){
  const cd = resp.headers.get('Content-Disposition') || '';
  const m = /filename\\*=UTF-8''([^;]+)/i.exec(cd);
  if (m){ try { return decodeURIComponent(m[1]); } catch(e){} }
  const m2 = /filename="([^"]+)"/i.exec(cd);
  return m2 ? m2[1] : '';
}
// 多选打包下载：已入库的按附件 id、未入库的按来源文件名，一次请求混着传
async function bundleDownload(){
  const sel = checkedFiles();
  if (!sel.length){ toast('请先勾选要下载的简历','warn'); return; }
  const names = sel.map(x=>x.name);
  const ids = sel.filter(x=>x.document_id).map(x=>Number(x.document_id));
  toast('正在打包 '+sel.length+' 份简历…','info');
  let r;
  try {
    r = await fetch('/api/sources/bundle', {method:'POST', headers: hdr(),
      body: JSON.stringify({names:names, document_ids:ids})});
  } catch(e){ toast('打包请求失败：'+e,'danger'); return; }
  if (!r.ok){
    let msg = '打包失败（HTTP '+r.status+'）';
    try { const j = await r.json(); msg = j.detail || msg; } catch(e){}
    toast(msg,'danger'); return;
  }
  const skipped = parseInt(r.headers.get('X-Skipped')||'0');
  const count = parseInt(r.headers.get('X-Batch-Count')||String(sel.length - skipped));
  let detail = '';
  if (skipped){
    // 详情走百分号编码（HTTP 头不能带非 ASCII 字符），这里解码还原成中文
    try {
      const raw = r.headers.get('X-Skipped-Detail') || '[]';
      detail = (JSON.parse(decodeURIComponent(raw))||[]).slice(0,2).join('；');
    } catch(e){}
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = zipNameFrom(r) || '简历原件打包.zip';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url), 5000);
  toast('已导出 '+count+' 份简历'+(skipped?('；'+skipped+' 份跳过：'+detail):''), skipped?'warn':'ok');
}
// 联系方式行：手机可拨号、邮箱可写信，旁边给一键复制。检索结果与人才库共用同一渲染
// 接口对"没有联系方式"用 "—" 占位，这里必须先归一掉，否则会渲染出 tel:— 这种空链接
function contactValue(v){
  const s = String(v==null ? '' : v).trim();
  return (s && s !== '—') ? s : '';
}
function contactLine(x){
  const p = contactValue(x && x.phone), m = contactValue(x && x.email);
  if (!p && !m) return '<span class="small" style="color:var(--ink-3)">未识别到联系方式</span>';
  const joined = (p||'') + (p&&m?' / ':'') + (m||'');
  return `<span class="contact">`
    + (p ? `<a href="tel:${esc(p)}" title="点击拨号">${esc(p)}</a>` : '')
    + (p && m ? ' · ' : '')
    + (m ? `<a href="mailto:${esc(m)}" title="点击发邮件">${esc(m)}</a>` : '')
    + `<button class="mini" onclick="copyText('${esc(joined)}')">复制</button></span>`;
}

async function boot(){
  META = await api('/api/meta');
  const su = document.getElementById('sideUser');
  // 左下角不再显示"本机模式 · 单 HR"这类模式标签（v1.24.9）：
  // 登录态才显示"登录：用户名"，本机模式留空。
  if (su) su.textContent = META.session.mode === 'local'
    ? '' : ('登录：' + META.session.username);
  const h = readHash();
  if (h) VIEW = VIEWS.some(x=>x[0]===h) ? h : 'pool';
  window.addEventListener('hashchange', () => go(readHash(), true));
  renderTabs();
  await refresh();
}

// 侧栏顺序（v1.15）：写邮件提到人才库之后——它是日常最高频动作之一，
// 归档沉到靠后（一年用一次，不该占黄金位）。
const VIEWS = [['brief','今日待办'],['pool','人才库'],['chat','智能助手'],
               ['mail','写邮件'],['org','岗位管理'],['archive','归档'],
               ['sys','系统配置']];
// 侧栏导航图标：内联 SVG（stroke 跟随文字色），不引外部图标库
const _I = p => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
  stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
const NAV_ICONS = {
  brief:   _I('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
  pool:    _I('<path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><circle cx="10" cy="7" r="4"/><path d="M21 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'),
  archive: _I('<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><path d="M10 12h4"/>'),
  chat:    _I('<path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2z"/>'),
  org:     _I('<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M9 8h.01M15 8h.01M9 12h.01M15 12h.01M9 16h.01M15 16h.01"/>'),
  mail:    _I('<path d="m22 2-7 20-4-9-9-4 20-7z"/><path d="M22 2 11 13"/>'),
  sys:     _I('<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>'),
};
function renderTabs(){
  document.getElementById('tabs').innerHTML = VIEWS.map(([k,l]) =>
    `<div class="navitem ${VIEW===k?'on':''}" onclick="go('${k}')" title="${l}">${NAV_ICONS[k]||''}<span>${l}</span></div>`).join('');
}
// 视图写进地址栏 #哈希：刷新后仍停在原来那一页，也能直接把某一页发给同事
function go(v, keepHash){
  // 切页面前先收掉弹层：完整档案 / JD 编辑都是整屏遮罩，
  // 留着会把新页面盖住，看起来像"点了导航没反应"。
  closeModal();
  VIEW = VIEWS.some(x=>x[0]===v) ? v : 'pool';
  if (!keepHash && location.hash !== '#'+VIEW) location.hash = VIEW;
  renderTabs(); refresh();   // body 的 main-on-chat 标记在 refresh() 里打
}
function readHash(){
  return (location.hash||'').replace(/^#/,'');
}

/* 收取/导入进行中的来源（'mailbox'|'folder'|null）。
   为什么声明在这里：refresh() 在下面，会读它来恢复按钮锁定。
   `let` 有暂时性死区——如果声明写在 doIngest 附近，
   refresh 首次执行时读它会直接抛 ReferenceError，页面当场白屏。 */
let __ingesting = null;
/* 导入弹窗秒表的 interval 句柄。同样必须提前声明：
   ingestStartModal() 定义在 ingestStopTick() 调用点之前，
   但赋值/读取散布在三个函数里，放一起最不容易踩死区。 */
let __ingestTick = null;

async function refresh(){
  // v1.24.20：聊天页要把页面滚动收进 .chatlog，否则底部多出一截空白。
  // 标记打在这里而不是 go()：**refresh() 是渲染每一页的必经之路**，
  // 首屏初始化走的是 readHash() 而不是 go()（实测 go() 里打首屏就漏了这一下，
  // body 上没有 main-on-chat，整套 CSS 静默不生效）。
  document.body.classList.toggle('main-on-chat', VIEW === 'chat');
  // 顶部统计卡片只在「人才库」展示（v1.25.6：今日待办页不再重复——
  // 两页顶部摆同一排数字，HR 反馈"和人才库重复的东西不需要展示"）。
  // 其他页面放一排数字只是噪音：看岗位管理或写邮件时，那排总数帮不上任何忙。
  const showStats = (VIEW === 'pool');
  const statsBox = document.querySelector('.grid-stats');
  if (statsBox) statsBox.style.display = showStats ? '' : 'none';
  if (showStats) renderStats(await api('/api/stats'));
  if (VIEW==='brief') await viewBrief();
  else if (VIEW==='pool') await viewPool();
  else if (VIEW==='archive') await viewArchive();
  else if (VIEW==='chat') await viewChat();
  else if (VIEW==='org') await viewOrg();
  else if (VIEW==='mail') await viewMail();
  else if (VIEW==='sys') await viewSys();
  // 人才库重渲染会把按钮区的 HTML 整个换掉，忙碌状态会被冲掉。
  // 这里按当前标志恢复锁定，否则"进行中"的按钮会变回可点，用户能重复提交。
  if (__ingesting) setIngestBusy(true, __ingesting);
}

/* 统计区（v1.24.14 优化）：分「待办层」与「库存层」。

   原来的 10 个数字同权重平铺，HR 进门不知道该先看哪个。
   现在的分法依据是**这个数字会不会要求 HR 动手**：
     待办层 = 需要你处理的（A 档待约面 / 待你确认 / 待确认提案）→ 大号数字
     库存层 = 背景事实（总人数、投递数、附件数、档位分布）→ 小号密排 + 一条档位比例条
   「待 HR 确认」为 0 时自动弱化——没有待办是好消息，不该继续用红色抢注意力。 */
function renderStats(s){
  if (!s || s.__http_error) return;
  const t = s.tiers || {};
  const n = v => (v == null ? 0 : v);
  const sum = n(t.A) + n(t.B) + n(t.C) + n(t.D);

  // 待办层：数字大、颜色语义明确；**每张卡都可点**（v1.25.6，HR 要求）——
  // 与右侧环同一套"数字即入口"的心智：点一下直接到该去的地方。
  const acts = [
    card('A 档 · 优先面试', t.A, 'var(--ok)', n(t.A) > 0,
         '点击 → 在人才库筛出 A 档', "TAB='A';STAGE_F='';poolPageReset();go('pool')"),
    card('待你确认', s.pending, 'var(--bad)', n(s.pending) > 0,
         '点击 → 筛出未复核的人', "TAB='UNCONFIRMED';STAGE_F='';poolPageReset();go('pool')"),
    card('待确认提案', s.proposals_pending, 'var(--bad)', n(s.proposals_pending) > 0,
         '点击 → 查看并逐条处理', 'openProposals()'),
    // v1.25.6 二改：信息缺失上移第一行（它是"要动手"的事，不是库存），且可点穿透。
    card('信息缺失', s.needs_review, 'var(--warn)', n(s.needs_review) > 0,
         '点击 → 在人才库筛出信息缺失（缺字段）的投递；打开档案可看到缺什么并更正',
         "TAB='REVIEW';STAGE_F='';poolPageReset();go('pool')"),
  ].join('');

  // 库存层：小而安静
  const rest =
    card('候选人数', s.people, 'var(--ink)') +
    card('投递数',   s.applications, 'var(--ink)') +
    card('简历附件', s.documents, 'var(--ink)') +
    // v1.25.6 二改：原"信息缺失"上移第一行；这里换成"近 7 天新增简历"（库存动态）。
    card('近 7 天新增简历', s.docs_7d, 'var(--ink)', false,
         '最近 7 天新导入的简历份数（按简历接收日期统计）');

  // 投递进度环（v1.25.6，用户要求"展示人才库每个人进度"）——统计区右侧环形比例图：
  //   · 每段 = 一个投递阶段（新投递 → 已联系 → 初面 → 复面 → 待offer → 已入职 → 已结束）；
  //   · 悬停看人数；点击一段（或图例一行）**穿透到人才库并按该阶段筛选**；
  //   · 点击时重置档位筛选（TAB='ALL'），避免"两个筛选叠加后人数变少"的困惑。
  const STAGE_COLOR = {
    '新投递': '#3370ff', '已联系': '#0ea5a5', '初面': '#f0b429', '复面': '#ef7c3a',
    '待offer': '#9254de', '已入职': '#12b76a', '已结束': '#a7b0bf',
  };
  const stageRing = (() => {
    const st = s.stages || {};
    const segs = STAGES.map(k => [k, n(st[k]), STAGE_COLOR[k] || '#a7b0bf']);
    const total = segs.reduce((a, x) => a + x[1], 0);
    if (!total) {
      return `<div class="tier-note" style="flex:none">投递进度：暂无（还没有投递）</div>`;
    }
    const R = 26, CIRC = 2 * Math.PI * R;
    let acc = 0;
    const arcs = segs.filter(x => x[1] > 0).map(([k, v, color]) => {
      const len = CIRC * v / total;
      const el = `<circle class="td-seg" r="${R}" cx="34" cy="34" fill="none"
          stroke="${color}" stroke-width="11"
          stroke-dasharray="${len} ${CIRC - len}" stroke-dashoffset="${-acc}"
          transform="rotate(-90 34 34)"
          onclick="STAGE_F='${k}';TAB='ALL';poolPageReset();go('pool')"
        ><title>${k}：${v} 人 —— 点击筛选</title></circle>`;
      acc += len;
      return el;
    }).join('');
    const legend = segs.map(([k, v, color]) =>
      `<div onclick="STAGE_F='${k}';TAB='ALL';poolPageReset();go('pool')"
            title="${k}：${v} 人 —— 点击筛选"
            style="cursor:pointer;display:flex;align-items:center;gap:5px;font-size:13px;color:var(--ink-2)">
        <i style="width:8px;height:8px;border-radius:2px;background:${color};display:inline-block"></i>
        ${k} ${v}
      </div>`).join('');
    return `<div class="tier-donut" title="投递进度（点环或图例 → 筛出该阶段）">
      <svg width="128" height="128" viewBox="0 0 68 68" role="img"
           aria-label="投递进度：${segs.map(x => x[0] + ' ' + x[1]).join('、')}">
        ${arcs}
        <text x="34" y="34" text-anchor="middle" dominant-baseline="central"
              font-size="13" font-weight="600" fill="var(--ink)">${total}</text>
      </svg>
      <div style="display:grid;gap:2px">${legend}</div>
    </div>`;
  })();

  // "最近导入"（v1.25.6，HR 要求）：右列环下的一行动态——给整块一点时间感。
  const lastDoc = s.last_doc;
  const recentLine = (lastDoc && lastDoc.at)
    ? `<div class="small" style="color:var(--ink-3)">最近导入：${esc(lastDoc.name)} · ${esc(ago(lastDoc.at))}</div>`
    : '';
  document.getElementById('stats').innerHTML =
    `<div class="stat-row">
       <div style="flex:1;min-width:0;max-width:calc(100% - 300px)">
         <div class="stat-act">${acts}</div>
         <div class="stat-rest">${rest}</div>
       </div>
       <div style="display:grid;gap:8px;justify-items:end;flex:none">
         ${stageRing}
         ${recentLine}
       </div>
     </div>`;
}
/* 相对时间（v1.25.6）：'YYYY-MM-DD HH:MM:SS' → "2 小时前"。用于"最近导入"。 */
function ago(ts){
  const t = new Date(String(ts || '').replace(' ', 'T'));
  if (isNaN(t.getTime())) return ts || '';
  const s = (Date.now() - t.getTime()) / 1000;
  if (s < 60) return '刚刚';
  if (s < 3600) return Math.round(s / 60) + ' 分钟前';
  if (s < 86400) return Math.round(s / 3600) + ' 小时前';
  return Math.round(s / 86400) + ' 天前';
}
/* hot=false 时该数字为 0，加 is-zero 让它自动褪成灰色；
   tip（可选）：鼠标悬停的解释，统计口径写在这里；
   go（可选，v1.25.6）：点击动作（内联 JS 字面量，如 "TAB='A';go('pool')"）——
   传了就整卡可点（手型光标），统计数字成为"入口"。 */
function card(k, v, c, hot, tip, go){
  const zero = (v == null || v === 0);
  return `<div class="stat${(zero && !hot) ? ' is-zero' : ''}"`
       + (tip?(' title="'+esc(tip)+'"'):'')
       + (go?(' onclick="'+go+'" style="cursor:pointer"'):'')
       + `><div class="k">${k}</div><div class="v" style="color:${c}">${v == null ? '—' : v}</div></div>`;
}

/* ------------------------------ 今日待办 ------------------------------ */
/* 与"报表"的区别：顶部那句 headline 是**判断**（今天最该关注什么），不是计数。
   模型不可用时后端会退回规则排序，并在 note 里如实说明是排出来的。 */
async function viewBrief(){
  const b = await api('/api/brief/today');
  const box = document.getElementById('view');
  if (b.__http_error){
    box.innerHTML = `<div class="card"><div class="note">待办摘要加载失败：${
      esc(b.detail||b.error||'')}</div></div>`;
    return;
  }
  const st = b.stats || {};
  const row = (k, v, tip) => `<div class="k">${k}</div><div>${v||0}${
    tip?` <span class="small">${esc(tip)}</span>`:''}</div>`;
  const prios = b.priorities || [];
  box.innerHTML = `
  <div class="panel">
    <h2>今日待办 <span class="small">${esc(b.date||'')} · ${
      b.cached ? '已生成' : '初次生成（模型判断版稍后可用）'}</span></h2>
    <div style="font-size:16px;font-weight:600;color:var(--accent-ink);margin:6px 0 4px">
      ${esc(b.headline||'')}</div>
    <div class="small">${esc(b.note||'')}</div>
    <div class="bar" style="margin-top:10px">
      <button onclick="runScan()">立即巡检一遍</button>
      <button onclick="rebuildBrief()">重算摘要（含模型判断）</button>
      <span id="briefMsg" class="small"></span>
    </div>
  </div>
  ${prios.length ? `<div class="card"><h2>优先处理（按建议顺序）</h2>
    ${prios.map((p,i)=>`
      <div style="padding:10px 0;border-bottom:1px solid var(--line)">
        <b>${i+1}. ${esc(p.title||'')}</b>
        ${p.why?`<div class="small" style="margin-top:4px">为什么现在做：${esc(p.why)}</div>`:''}
        ${p.action?`<div class="small">建议动作：${esc(p.action)}</div>`:''}
      </div>`).join('')}
  </div>` : ''}
  <div id="propsBox"></div>
  <div class="small" style="margin:10px 2px">${esc(b.disclaimer||'')}</div>`;
  renderPropsInto('propsBox');      // 待确认提案（原「提案与审计」页，已并入本页）
}

async function runScan(){
  const msg = document.getElementById('briefMsg');
  if (msg) msg.textContent = '巡检中…';
  const r = await api('/api/proactive/scan', {method:'POST'});
  if (r.__http_error || r.error){
    if (msg) msg.textContent = r.detail || r.error || '巡检失败';
    return;
  }
  toast(r.note || '巡检完成', r.created_count ? 'ok' : 'warn');
  if (msg) msg.textContent = r.note || '';
  refresh();
}

async function rebuildBrief(){
  const msg = document.getElementById('briefMsg');
  if (msg) msg.textContent = '重算中（会调用模型，可能较慢）…';
  const r = await api('/api/brief/rebuild?use_llm=1', {method:'POST'});
  if (r.__http_error || r.error){
    if (msg) msg.textContent = r.detail || r.error || '重算失败';
    return;
  }
  toast('已重算：' + (r.headline || ''), 'ok');
  refresh();
}

/* 招聘对象身份：校招模式下**只显应届/往届未就业，不显年限数字**。
   HR 口径（2026-10-10）：「直接默认都是应届生，不要做工作年限的分析以及展示」——
   校招里"能不能投"看的是身份，不是一个年限数字。 */
function expBadge(x){
  const e = x.exp_display;
  // 校招口径：**不展示工作年限**。
  // v1.25.6（HR 反馈）：默认**连"应届"也不展示**——招聘默认都是校招，
  // 每个人挂一枚绿色"应届"没有任何信息量。只有"往届未就业"这种
  // 值得提醒的例外才标出来，其余一律返回空（调用方按空串处理分隔符）。
  if (e && e.kind === 'past_idle') return expBadgeReal(e);
  return '';
}
function expBadgeReal(e){
  if (!e) return '应届';
  const color = e.kind === 'fresh' ? 'var(--ok)'
              : e.kind === 'past_idle' ? 'var(--warn)'
              : e.kind === 'unknown' ? 'var(--ink-3)' : 'var(--ink)';
  return `<span style="color:${color}" title="${esc(e.note||'')}">${esc(e.label)}</span>`;
}

/* 学历达标标记：学历是岗位的硬门槛，不能只甩一个"本科"让人自己去比。
   三态显示——达标（绿）、不达标（红、加粗，一眼可见）、未识别（橙，不能替人下结论）。 */
function eduBadge(x){
  const e = x.edu_check;
  const raw = x.edu_level || '—';
  if (!e) return esc(raw);
  if (e.unknown){
    return `<span title="简历里没识别出学历，无法与岗位要求（${esc(e.required)}）比对">` +
           `${esc(raw)} <span style="color:var(--warn)">? 待判定</span></span>`;
  }
  if (e.ok){
    return `${esc(raw)} <span class="small" style="color:var(--ok)"> 达 ${esc(e.required)} 线</span>`;
  }
  return `<span style="color:var(--bad);font-weight:600" ` +
         `title="岗位【${esc(e.job_title||'')}】要求 ${esc(e.required)} 及以上">` +
         `${esc(raw)} ✗ 低于要求（${esc(e.required)}）</span>`;
}

/* ------------------------------ 人才库 ------------------------------ */
/* 系统自动分析块：与下方「推荐理由」（规则模板句）**分开呈现**——
   两者来源不同，混在一起 HR 会分不清哪句是规则算的、哪句是模型读简历得出的。 */
function insightBlock(x){
  const ins = x.insight;
  if (!ins){
    return `<div class="small" style="color:var(--ink-3);margin-top:8px">` +
      `自动分析生成中…（稍后刷新，或在人才库卡片上点「重新分析」做完整重跑）</div>`;
  }
  const src = ins.source === 'rule_fallback' ? '规则降级（模型不可用）'
            : ins.source === 'auto_profile'  ? '未归岗 · 简历画像'
            : ins.source === 'manual'        ? '手动重算' : '进门即分析';
  const ev = (ins.evidence||[]).slice(0,2)
    .map(e => `[${e.skill}] ${(e.quote||'').slice(0,28)}`).join(' ｜ ');
  // 底色用 --accent-hi（强调色的最浅一档）。原来这里是 #f2f7ff——上一版遗留的
  // 冷蓝，和强调色不同色温，洞察块因此比周围明显"偏紫"。
  return `<div style="margin-top:10px;background:var(--accent-hi);border-left:3px solid var(--accent);
                      padding:8px 10px;border-radius:0 6px 6px 0">
    <b>系统自动分析</b> <span class="small">${esc(src)}${ins.model?' · '+esc(ins.model):''}</span>
    <div style="margin-top:4px">${esc(ins.summary||'')}</div>
    ${ins.business_direction ? `<div class="small" style="margin-top:4px">业务方向：
      <b style="color:var(--accent-ink)">${esc(ins.business_direction)}</b>
      <span class="small" style="color:var(--ink-3)">（模型从简历提炼，仅展示）</span></div>` : ''}
    ${(ins.reasons||[]).length ? `<div class="small">依据：${esc(ins.reasons.join('；'))}</div>` : ''}
    ${(ins.risks||[]).length ? `<div class="small">风险：${esc(ins.risks.slice(0,2).join('；'))}</div>` : ''}
    ${ev ? `<div class="small">证据：${esc(ev)}</div>` : ''}
    ${tierSourceLine(x)}
  </div>` + '';
}

/* 档位来源（v1.12）：原来的「档位依据（纯规则、评分拆解）」面板已删除——
   加权打分已从系统里移除（HR 反馈那是噪音），再展示"分值拆解"等于展示一套不存在的算法。
   现在档位只有两个来源：① 学历门槛（不达标→D，规则可复现）；② 模型分析给的 A/B/C。
   所以这里只留一行来源说明，真正的理由在「系统自动分析」的文字里（模型给的）。
   保留技能命中/缺失与专业方向——它们是反幻觉的凭据，不是打分的中间量。 */
function tierSourceLine(x){
  const t = x.tier_detail;
  if (!t) return '';
  const mj = t.major || {};
  const src = (t.tier === 'D') ? '学历门槛（规则判定，可复现）'
            : (t.tier ? '模型分析判断' : '待分析（模型尚未给出结论）');
  const hits = (t.hit || []).map(esc).join('、') || '—';
  const miss = (t.miss || []).map(esc).join('、') || '—';
  return `<details style="margin-top:6px">
    <summary class="small" style="cursor:pointer;color:var(--accent-ink)">
      档位来源：${esc(src)} · 建议 ${esc(t.tier || '待分析')}</summary>
    <div class="small" style="margin-top:4px;line-height:1.75">
      档位不再由分数计算：<b>学历不达标直接判 D</b>，其余档位由模型读简历后判断。<br>
      命中技能：${hits}<br>
      缺失技能：${miss}
      ${(t.miss_custom||[]).length?`<br>岗位自定义要求（本体未收录、按文字比对）未命中：${esc((t.miss_custom||[]).join('、'))}`:''}
      ${mj.note?`<br>专业方向：${esc(mj.note)}`:''}
      ${(t.risks||[]).length?`<br>风险提示：${esc(t.risks.join('；'))}`:''}
    </div></details>`;
}

async function reanalyze(cid, full){
  full = (full === undefined) ? true : !!full;
  const out = document.getElementById('out-'+cid);
  const btns = document.querySelectorAll(`[data-reanalyze="${cid}"]`);
  btns.forEach(b => { b.disabled = true; b.dataset.old = b.textContent; b.textContent = '重新分析中…'; });
  if (out){
    out.innerHTML = '<div class="note">完整重新分析中：重新解析原件 → 模型重抽字段 → '
      + '重建技能 → 重新归岗 → 重判建议档 → 重算分析。<b>约 10 秒</b>，请勿关闭页面。</div>';
  }
  let r;
  try {
    r = await api('/api/candidates/'+cid+'/reanalyze?full=' + (full?'true':'false'),
                  {method:'POST'});
  } finally {
    btns.forEach(b => { b.disabled = false; b.textContent = b.dataset.old || '重新分析'; });
  }
  if (r.__http_error || r.error){
    if (out) out.innerHTML = `<div class="danger">重新分析失败：${esc(r.detail||r.error||'')}
      ${r.hint?('<br><span class="small">'+esc(r.hint)+'</span>'):''}</div>`;
    toast(r.detail || r.error || '重新分析失败', 'danger');
    return;
  }
  // 把"到底做了什么"逐条列出来——HR 点的是"重新分析"，
  // 就有权知道它到底重跑了哪几步、哪些字段变了（否则只能凭感觉）
  const cs = r.changes || {};
  const keys = Object.keys(cs);
  const summaryHtml = `<div class="note" style="background:var(--ok-soft)">
      <b>完成</b>（抽取方式：${esc(r.extract_mode||'—')}${r.tier_suggested?' · 建议档 '+esc(r.tier_suggested):''}）
      <div style="margin-top:4px">${(r.steps||[]).map(s=>'· '+esc(s)).join('<br>')}</div>
      ${keys.length ? `<div class="small" style="margin-top:4px">字段变化：` +
          keys.map(k=>`${esc(k)}：${esc(String(cs[k][0]??'（空）'))} → <b>${esc(String(cs[k][1]??''))}</b>`).join('；')
          + `</div>` : ''}
      ${(r.kept_hr||[]).length ? `<div class="small" style="margin-top:4px">已保留人工更正：
          ${esc((r.kept_hr||[]).join('、'))}（不做覆盖）</div>` : ''}
    </div>`;
  const fromModal = document.getElementById('modal').classList.contains('on');
  if (fromModal){
    // 弹层里的按钮：重开档案让新值生效，再把结果面板放回去
    // （否则页面上还是旧字段，HR 会以为没跑成功）
    await showDetail(cid);
  } else {
    // 列表里的按钮：先刷新让新值上屏，**再把结果面板塞回去**。
    // 原来只填面板就 refresh()，卡片重渲染把面板一起冲没了 ——
    // 表现就是"点了没反应"（请求其实发出去了，14 秒后静默结束）。
    refresh();
  }
  // 候选人可能已被筛选条件排除、不在这一页 → 面板没处放，就贴到页面顶部并保留
  const box = document.getElementById('out-'+cid);
  if (box){ box.innerHTML = summaryHtml; }
  else {
    // 候选人被筛选排除、不在这一页 → 没有卡片可挂，改成**居中弹窗**（v1.24.9）。
    // 原来贴到 #alerts（页面顶部），操作在下方时看不见，等于白做。
    const host = document.getElementById('popLayer') || document.getElementById('alerts');
    const d = document.createElement('div');
    d.className = 'pop ok';
    d.style.cssText = 'padding:12px 14px;text-align:left;cursor:default';
    d.innerHTML = summaryHtml;
    host.appendChild(d);
    setTimeout(()=>d.remove(), 60000);
  }
  toast(r.note || '已完成完整重新分析', 'ok');
}

async function viewPool(){
  // 管道条与人选列表各取一份：/api/pipeline 提供各阶段人数与明细（嵌入本页顶部），
  // /api/candidates 提供当前筛选 + 分页的候选人卡片，两者互不影响。
  const [c, p] = await Promise.all([
    api('/api/candidates?tier=' + encodeURIComponent(TAB)
      + '&kw=' + encodeURIComponent(KW) + '&gender=' + encodeURIComponent(GENDER)
      + '&education=' + encodeURIComponent(EDU_MIN)
      + '&univ=' + encodeURIComponent(UNIV)
      + '&stage=' + encodeURIComponent(STAGE_F)
      + '&page=' + POOL_PAGE + '&page_size=' + POOL_SIZE),
    api('/api/pipeline')
  ]);
  ITEMS = c.items || [];
  const pg = c.paging || null;              // 后端算好的页码/总数（页大小不影响统计口径）
  const gf = c.gender_filter || {}, gfOn = !!gf.enabled;
  const isw = c.insight_switch || {enabled: true, pending: 0};   // v1.13.4 入库即分析开关
  const host = document.getElementById('view');
  const n = {ALL:(pg?pg.total:(c.items||[]).length), REVIEW:0, UNCONFIRMED:0};
  const tabs = [['ALL','全部'],['A','A 优先面试'],['B','B 建议面试'],['C','C 储备'],
                ['D','D 暂不匹配'],['REVIEW','信息缺失（缺字段）'],['UNCONFIRMED','未复核']];
  // 性别筛选只在开关打开时出现：开关关着还摆一个筛选项，等于诱导所有人按性别筛。
  const facets = c.gender_facets || {};
  const gsel = !gfOn ? '' : `<span class="small" style="margin-left:6px">性别</span>
    <select onchange="GENDER=this.value;poolPageReset();refresh()">
      <option value="">不限</option>
      ${['男','女','未标注'].map(g=>`<option value="${g}" ${GENDER===g?'selected':''}>${g}（${facets[g]==null?0:facets[g]}）</option>`).join('')}
    </select>`;
  // 初筛（v1.7.3）：最低学历（≥门槛，"无法判定"不命中但下方会提示人数）
  // 与院校层次（985 ⊂ 211，选 211 时 985 也算；下拉人数是筛选前口径，对得上共 N 人）。
  // 两者都是岗位相关硬条件，与性别不同，不需要开关约束。
  const uniF = c.uni_facets || {};
  const eduSel = `<span class="small" style="margin-left:6px">学历</span>
    <select onchange="EDU_MIN=this.value;poolPageReset();refresh()">
      ${[['','不限'],['大专','大专及以上'],['本科','本科及以上'],['硕士','硕士及以上'],['博士','博士']]
        .map(([v,l])=>`<option value="${v}" ${EDU_MIN===v?'selected':''}>${l}</option>`).join('')}
    </select>`;
  // v1.16.1：阶段筛选。管道里"还有 N 人 · 看全部"跳过来时靠它落地——
  // 后端 /api/candidates 早就支持 stage 参数，只是界面一直没给入口。
  const stageSel = `<span class="small" style="margin-left:6px">阶段</span>
    <select onchange="STAGE_F=this.value;poolPageReset();refresh()">
      <option value="">不限</option>
      ${STAGES.map(k=>`<option value="${k}" ${STAGE_F===k?'selected':''}>${k}</option>`).join('')}
    </select>`;
  const uniSel = `<span class="small" style="margin-left:6px">院校</span>
    <select onchange="UNIV=this.value;poolPageReset();refresh()">
      <option value="">不限</option>
      <option value="985" ${UNIV==='985'?'selected':''}>985（${uniF['985']==null?0:uniF['985']}）</option>
      <option value="211" ${UNIV==='211'?'selected':''}>211（${uniF['211']==null?0:uniF['211']}，含 985）</option>
    </select>`;
  const gtip = (!gfOn && gf.requested)
    ? `<div class="warn" style="margin-top:8px">性别筛选未生效：${esc(gf.why||'')}</div>` : '';
  // 学历"无法判定"的人被门槛挡下时必须说出来，不能让人静默消失
  const ef = c.edu_filter || {};
  const eduWarn = (ef.applied && ef.hidden_unknown > 0)
    ? `<div class="warn" style="margin-top:8px">有 <b>${ef.hidden_unknown}</b> 人学历无法判定，未计入「${esc(ef.min)}及以上」筛选结果；选「不限」可看到全部。</div>` : '';
  host.innerHTML = `
  <div class="panel">
    <!-- v1.25.6：工具栏重排——找（搜索/筛选）、收（导入）、析（分析）三簇左对齐连续排列，
         竖线分隔，不再用 flexbetween 把两组拉到两端（中宽度下会形成大片空隙、显散） -->
    <div class="bar" style="flex-wrap:wrap;row-gap:8px">
        <input id="kwBox" placeholder="搜索姓名 / 院校 / 专业 / 技能"
               style="width:280px" value="${esc(KW)}"
               onkeydown="if(event.key==='Enter'){doSearch()}">
        <button class="btn-primary" onclick="doSearch()">搜索</button>
        <button onclick="KW='';GENDER='';EDU_MIN='';UNIV='';poolPageReset();refresh()">清空</button>
        ${gsel}${eduSel}${uniSel}${stageSel}
        <!-- v1.25.6：**强制换行**——导入组固定从第二行开始（HR 要求"收取邮箱简历
             应该在第二行"），不再看窗口宽度随机挤到第一行末尾。
             flex-basis:100% 的空元素在 flex-wrap 容器里起 line-break 作用。 -->
        <span style="flex-basis:100%;height:0"></span>
        <button id="btnIngestMailbox" onclick="doIngest('mailbox')">收取邮箱简历</button>
        <button id="btnIngestFolder" onclick="doIngest('folder')">导入本地文件夹</button>
        <button onclick="document.getElementById('upFile').click()"
                title="手机拍的纸质简历照片、扫描件，直接选文件上传入库（不用先改配置目录）">上传简历照片/文件</button>
        <input id="upFile" type="file" multiple hidden
               accept=".jpg,.jpeg,.png,.bmp,.webp,.pdf,.docx,.doc,.txt"
               onchange="doUpload(this.files)">
        <button onclick="exportCsv()">导出 CSV</button>
        <span class="vdiv"></span>
        <span class="small">入库即分析</span>
        <label class="small" style="display:flex;align-items:center;gap:4px;cursor:pointer"
               title="开启：新简历入库后自动分析一次（每人约 2-8 秒、消耗模型额度）；关掉：入库不调模型，改由你按需批量补">
          <input type="checkbox" ${isw.enabled?'checked':''}
                 onchange="setAutoInsight(this.checked)"> ${isw.enabled?'开':'关'}
        </label>
        <!-- v1.25.6：没有待分析的人时**隐藏**（不用一个灰色禁用按钮占版面，界面更干净；
             JS 端 analyzePendingBatch 已做 !btn 防御，隐藏不影响引用） -->
        <button id="btnAnalyzePending" data-pending="${isw.pending}"
                onclick="analyzePendingBatch()" ${isw.pending?'':'disabled style="display:none"'}>
          ${isw.pending ? ('分析待分析的人（'+isw.pending+'）') : ''}
        </button>
        <button id="btnReanalyzeBatch" onclick="reanalyzeBatch()"
                title="把「抽取方式=规则通道」的档案按当前配置完整重跑一遍：重读原件 → 模型重抽字段 → 重建技能 → 重新归岗 → 重判建议档。HR 已定的档位/阶段/岗位不动，人工更正过的字段保留。">
          批量按模型重跑
        </button>
    </div>
    <div class="bar" style="margin-top:8px">
      ${(()=>{
        // v1.22.1：固定列 2018–2050，默认当年（避免"近4年"窗口明年错位）。
        // 下拉**只显示年份**——"年以前"的含义由右边按钮说全，不用重复两遍；
        // 说明文字也不常驻（悬停「批量归档」可见）。
        const y = new Date().getFullYear();
        const ys = [];
        for (let v=2018; v<=2050; v++) ys.push(v);
        return `<div class="arch-bar"
            title="按年使用：新一年开始时把旧简历整批收起。归档不等于删除——档案/投递/附件/审计都保留，可随时取消归档，也可随时彻底删除。">
          <span class="arch-label">批量归档</span>
          <button onclick="archiveBatch(null,true)">归档勾选的人</button>
          <span class="arch-div"></span>
          <select id="poolArchYear" onchange="pickArchYear(this.value)">
            ${ys.map(v=>`<option value="${v}"${v===y?' selected':''}>${v}</option>`).join('')}
          </select>
          <span class="arch-suffix">年以前</span>
          <button id="btnArchByYear" class="btn-primary" onclick="archiveByYear()">
            归档 ${y} 年以前的投递</button>
        </div>`;
      })()}


    </div>
    ${gtip}
    ${eduWarn}
  </div>
  ${pipeBoardHtml(p)}
  <!-- v1.25.6：档位筛选条移到投递管道**下面**（HR 要求：先看管道全局，
       再在这里筛列表——而不是挤在工具栏面板里） -->
  <div class="tabs" style="margin:10px 0 12px">
    ${tabs.map(([k,l])=>`<div class="tab ${TAB===k?'on':''}" onclick="TAB='${k}';poolPageReset();refresh()">${l}${k==='ALL'?(' '+n.ALL):''}</div>`).join('')}
  </div>
  ${ITEMS.length ? ITEMS.map(cardHtml).join('') : '<div class="card">没有符合条件的候选人。到「导入与来源」收一次简历试试。</div>'}
  ${pgBar(pg)}`;
}
// 分页条（v1.7.1）：每页 10 人。"共 N 人"始终是**筛后总数**（后端算好），不是本页条数，
// 避免出现"明明写着 3/2 页、总数却写 10"这种口径打架。只有一页时不渲染，省一行噪音。
function pgBar(pg){
  if (!pg || (pg.total_pages||1) <= 1) return '';
  const prev = pg.page > 1
    ? `<button onclick="POOL_PAGE=${pg.page-1};refresh()">上一页</button>`
    : `<button disabled>上一页</button>`;
  const next = pg.page < pg.total_pages
    ? `<button onclick="POOL_PAGE=${pg.page+1};refresh()">下一页</button>`
    : `<button disabled>下一页</button>`;
  return `<div class="card" style="display:flex;align-items:center;gap:10px">
    <span class="small">共 ${pg.total} 人 · 第 ${pg.page} / ${pg.total_pages} 页（每页 ${pg.page_size} 人）</span>
    ${prev}${next}
    <span class="small" style="margin-left:2px">跳至</span>
    <input id="pgJump" type="number" min="1" max="${pg.total_pages}" placeholder="页码"
           style="width:70px" onkeydown="if(event.key==='Enter')jumpPoolPage()">
    <button onclick="jumpPoolPage()">跳转</button>
    <span class="small" style="color:var(--ink-3)">导出 CSV 不受分页影响，始终导出当前筛选的全部人</span>
  </div>`;
}
// 跳转指定页：只做正数校验，越界交给后端夹取（page 会被夹到 [1, total_pages]），
// 这样输入 999 也能落到末页而不是报错——翻页是阅读动作，不该用报错打断。
function jumpPoolPage(){
  const el = document.getElementById('pgJump');
  const n = parseInt(el && el.value, 10);
  if (!n || n < 1){ toast('请输入要跳转的页码','warn'); return; }
  POOL_PAGE = n;
  refresh();
}
// 院校层次标签（v1.7.3）：来自 config/universities.json 名单匹配，只展示、不参与档位判定；
// 985/211 筛选用同一个字段（uni_tier），标签与筛选口径天然一致。
function uniTag(t){
  if (!t) return '';
  return `<span class="chip" style="color:var(--warn);background:var(--warn-soft)"
    title="按教育部 985/211 名单匹配院校名（含常见简称与校区后缀），仅展示标签，不参与档位判定">${esc(t)}</span>`;
}
function cardHtml(x){
  const t = x.tier_effective || 'D';
  const [fg,bg] = TIER_COLOR[t] || TIER_COLOR.D;
  const job = x.job_title || null;
  const sug = x.job_suggestion || null;
  // 建议岗位（v1.5）：入库时就把**每个在招岗位**的 JD 试算了一遍，取最匹配的那个，
  // 并且卡片上的档位就是**按这个岗位的尺子**判的——所以这里没有"材料类默认尺子"
  // 造成的错标（一位 Java 工程师不会再被钛合金尺子打成 D 档）。
  const sugChip = (!job && sug)
    ? `<span class="chip" style="color:var(--accent-ink);background:var(--accent-soft)"
         title="模型判断这份简历最像哪个在招岗位（结论已落库，展示时不再调用模型）；采纳后才真正归岗">建议岗位：${esc(sug.title)}${sug.reason?'（'+esc(sug.reason)+'）':''}</span>`
    : (job ? '' : (x.job_suggestion_missing
        ? `<span class="chip" style="color:var(--ink-3);background:var(--surface-2)"
             title="系统还没为这份简历判断过建议岗位（库里没有结论）。点「判断建议岗位」让模型判断一次，结论会存下来">尚未判断建议岗位</span>`
        : `<span class="chip" style="color:var(--ink-3);background:var(--surface-2)"
             title="模型从在招岗位里也没判断出最像哪个（例如跨行业简历）">模型未判断出对应岗位</span>`));
  // 「所属岗位待指定」直接做成可点的入口：没有它，HR 只能看着标签干瞪眼——
  // 原来"归岗"只在系统给出建议时才有按钮，模型不启用时完全没有入口（实测反馈）。
  const jobTag = job
    ? `<span class="chip job-tag">${esc(job)}</span>`
    : `<span class="chip job-tag pending" style="cursor:pointer" title="点击指定岗位"
         onclick="assignJobPick(${x.id})">所属岗位待指定（点此指定）</span>${sugChip}`;
  // 性别标签：只在简历**明写**时才有值（系统不做推断），提示里说明它不参与档位判定
  const genderTag = (x.gender||'').trim()
    ? `<span class="chip" style="color:var(--ink-2);background:var(--surface-2)" title="来自简历明写标签，不参与档位判定">${esc(x.gender)}</span>`
    : '';
  const hits = (x.hits||[]).map(s=>`<span class="chip" style="color:var(--ok);background:var(--ok-soft)">命中 ${esc(s)}</span>`).join('');
  const miss = (x.miss||[]).map(s=>`<span class="chip" style="color:var(--warn);background:var(--warn-soft)">缺 ${esc(s)}</span>`).join('');
  // 状态标签按**实际情况**显示，不再把库里的默认值「待确认」原样贴上：
  // 未归岗的投递没有档位可确认（显示"待归岗"），已归岗但档位还没出来的显示"待分析"。
  // 每个标签都指向一个明确的下一步，鼠标悬停能看到该做什么（title=status_hint）。
  const _st = x.status_display || '待确认';
  const _stStyle = {
    '已确认': 'color:var(--ok);background:var(--ok-soft)',
    '待确认': 'color:var(--warn);background:var(--warn-soft)',
    '待分析': 'color:var(--accent-ink);background:var(--accent-soft)',
    '待归岗': 'color:var(--warn);background:var(--warn-soft)',
  }[_st] || 'color:var(--ink-2);background:var(--surface-2)';
  // 待确认 → 顺手给一个「复核」按钮：认同默认档位的人点一下就行，不必改档位
  const _rev = (x.status_display === '待确认' && x.application_id)
    ? ` <button class="mini" onclick="markReview(${x.application_id},false)"
         title="表示这个人的信息已核对完毕（不改档位）">复核</button>` : '';
  const conf = (_st
    ? `<span class="chip" style="${_stStyle}" title="${esc(x.status_hint||'')}">${esc(_st)}</span>` + _rev
    : '');
  // 两条线分开显示（v1.24.9）：解析失败 ≠ 信息缺失。
  // 解析失败 → "简历读不出"（要去修文件/看原件）；信息缺失 → "缺：年限、技能"（补字段即可）
  const _miss = x.missing_fields || [];
  const rev = x.parse_failed
    ? '<span class="chip" style="color:var(--bad);background:var(--bad-soft)" title="简历原文未能解析，需要人工查看附件原件">简历读不出</span>'
    : (x.needs_review
        ? (_miss.length
            ? `<span class="chip" style="color:var(--warn);background:var(--warn-soft)" title="简历读出来了，但这些字段没抽到：${esc(_miss.join('、'))}，可手动更正">缺：${esc(_miss.join('、'))}</span>`
            : '<span class="chip" style="color:var(--warn);background:var(--warn-soft)" title="这条投递有字段没抽到；打开完整档案会列出具体缺什么（姓名/年限/技能），并提供更正入口">信息缺失 · 点开档案核对</span>')
        : '');
  const stage = x.stage || '新投递';
  const tiers = ['A','B','C','D'].map(k=>`<option value="${k}" ${k===t?'selected':''}>${k} · ${TIER_LABELS[k]}</option>`).join('');
  const stages = STAGES.map(k=>`<option value="${k}" ${k===stage?'selected':''}>${k}</option>`).join('');
  const skillChips = (x.skills||[]).slice(0,12).map(s=>`<span class="chip chip-skill">${esc(s)}</span>`).join('');
  const contact = contactLine(x);
  const scoreTip = (!job && sug)
    ? ` title="档位按「建议岗位 · ${esc(sug.title)}」的 JD 判断：学历不达标判 D，其余由模型给出"` : '';
  return `<div class="card" style="border-left-color:${fg}">
    <div class="row1">
      <input type="checkbox" class="pickChk" value="${x.id}" style="margin-right:10px">
      <div class="avatar">${esc((x.name||'?').slice(0,1))}</div>
      <div style="flex:1;min-width:0">
        <div class="nm">${esc(x.name||'未识别')} ${rev} ${genderTag} ${jobTag}</div>
        <div class="meta">${eduBadge(x)}${expBadge(x) ? (' · ' + expBadge(x)) : ''} ·
          ${esc(x.school||'—')}${uniTag(x.uni_tier)} · 阶段 ${esc(stage)} ·
          来源 ${esc(x.channel||'—')} · 投递 ${esc((x.applied_at||'').slice(0,10))}</div>
        <div class="meta" style="margin-top:2px"><b>联系方式</b>：${contact}</div>
      </div>
      <div class="badge"${scoreTip} style="color:${fg};background:${bg}"><b>${t}</b><span>${TIER_LABELS[t]}</span></div>
    </div>
    <div style="margin-top:10px">${hits}${miss}${conf}</div>
    <div style="margin-top:8px">${skillChips}</div>
    <div class="evs">${(x.skill_detail||[]).filter(k=>k.evidence).slice(0,3).map(k=>
      `<div class="ev">证据[${esc(k.name)}]：${esc(k.evidence)}</div>`).join('')}</div>
    ${insightBlock(x)}
    <div class="why">推荐理由：${esc((x.reasons||[]).join('；')||'—')}</div>
    <div class="acts">
      <select onchange="setTier(${x.application_id},this.value)">${tiers}</select>
      <select onchange="setStage(${x.application_id},this.value)">${stages}</select>
      <button onclick="showDetail(${x.id})">完整档案</button>
      <button data-reanalyze="${x.id}" onclick="reanalyze(${x.id},true)"
        title="完整重新分析：重新解析原件 → 模型重抽字段 → 重建技能 → 重新归岗 → 重判建议档 → 重算分析。约 10 秒。不动 HR 已定的档位/阶段/岗位。">重新分析</button>
      <button onclick="interview(${x.id})">面试提纲</button>
      ${sug ? `<span class="vdiv"></span>
      <button class="btn-primary" onclick="assignJob(${x.id},${sug.job_id},'${esc(sug.title)}')">采纳建议岗位</button>
      <button onclick="assignJobPick(${x.id})">换个岗位</button>`
      : (job ? '' : `<span class="vdiv"></span>
      ${x.job_suggestion_missing ? `<button id="sugjob-${x.id}" onclick="suggestJob(${x.id})">判断建议岗位</button>` : ''}
      <button class="btn-primary" onclick="assignJobPick(${x.id})">指定岗位</button>`)}
      <span class="vdiv"></span>
      <button class="btn-danger" onclick="archiveCandidate(${x.id},true)">归档</button>
    </div>
    <div id="pickjob-${x.id}"></div>
    <div id="out-${x.id}"></div>
  </div>`;
}
async function setTier(aid, tier){
  if (!aid) { toast('该候选人暂无投递记录', 'warn'); return; }
  const r = await api('/api/applications/'+aid+'/tier', {method:'POST', body:JSON.stringify({tier:tier})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'改档失败','danger'); return; }
  refresh();   // 卡片上的档位徽章就地变了，不再弹 toast（Hallmark Gate 16）
}
async function setStage(aid, stage){
  if (!aid) return;
  const r = await api('/api/applications/'+aid+'/stage', {method:'POST', body:JSON.stringify({stage:stage})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'推进失败','danger'); return; }
  // 阶段走到终点时后端会自动归档，提示要如实说明，否则 HR 会发现人「不见了」却不知为何
  toast(r.note || ('阶段已推进到「'+stage+'」'), r.auto_archived ? 'warn' : 'ok'); refresh();
}
async function showDetail(cid){
  const d = await api('/api/candidates/'+cid);
  if (d.__http_error){ toast(d.detail||'读取失败','danger'); return; }
  setModalTitle((d.name||'未识别') + ' · 完整档案',
    `<button class="btn-primary" onclick="saveFields(${d.id})"
      title="更正范围：姓名 / 学历 / 学校 / 专业 / 性别 / 电话 / 邮箱（技能与年限须以简历原文为准，不在这里改）">保存更正</button>`);
  const skills = (d.skills||[]).map(s=>`<span class="chip" style="color:${s.verified?'var(--accent)':'var(--ink-3)'};
      background:${s.verified?'var(--accent-soft)':'var(--surface-2)'}">${esc(s.name)}${s.verified?'':'(未核验)'}</span>`).join('');
  const evs = (d.skills||[]).filter(s=>s.evidence).slice(0,10).map(s=>
      `<div class="ev">${esc(s.name)}：${esc(s.evidence)}</div>`).join('');
  const apps = (d.applications||[]).map(a=>`<tr>
      <td>#${a.id}</td>
      <td>${esc(a.job_title||'待指定')}
        <button onclick="assignJobPick(${d.id},'pickjobD-${d.id}',${a.id})">${a.job_id ? '修改岗位' : '指定岗位'}</button></td>
      <td>${esc(a.channel||'—')}</td>
      <td>${esc((a.applied_at||'').slice(0,16))}</td>
      <td>${esc(a.tier_final||a.tier_suggested||'—')}</td><td>${esc(a.stage||'—')}</td>
      <td>${esc(a.status_display||a.status||'—')}
        <button class="mini" onclick="markReview(${a.id},${(a.status_display==='待确认')?'false':'true'})"
          title="${(a.status_display==='待确认')?'表示这个人的信息已核对完毕（不改档位）':'已复核，点此撤销复核'}">${(a.status_display==='待确认')?'复核':'撤销复核'}</button>
      </td></tr>`).join('');
  const docs = (d.documents||[]).map(x=>{
      const okFile = !!x.archived_path;
      const acts = okFile
        ? `<button onclick="openDoc(${x.id},1)">在线打开</button>
           <button class="btn-primary" onclick="openDoc(${x.id},0)">下载原件</button>`
        : '<span class="small bad-txt" title="该附件的原件文件没有归档，无法打开或下载；可重新导入简历以补上留档">原件未留档</span>';
      return `<tr><td>${esc(x.file_name)}<div class="small">${esc(x.mime||'')} ·
         ${x.size?Math.round(x.size/1024)+' KB':''}</div></td>
      <td>${esc(x.parse_engine||'—')}<div class="small">${x.text_len?x.text_len+' 字':''}</div></td>
      <td>${x.parse_ok?'<span class="ok-txt">已解析</span>':'<span class="warn-txt" title="原文未能解析，需人工查看附件原件">简历读不出</span>'}</td>
      <td>${esc((x.received_at||'').slice(0,16))}</td>
      <td class="small">${esc(x.archived_path||'—')}</td>
      <td class="bar">${acts}</td></tr>`;
    }).join('');
  const dup = (d.duplicates||[]).length ? `<div class="warn" style="margin:8px 0">
      系统提示以下档案疑似重复（**未自动合并**，需 HR 判断）：
      ${(d.duplicates||[]).map(x=>`#${x.id} ${esc(x.name)}（${esc(x.reason)}）`).join('；')}
      </div>` : '';
  document.getElementById('mBody').innerHTML = `
    <div class="kv">
      <div class="k">基本信息</div>
      <div style="background:var(--surface-2);border-radius:8px;padding:10px 12px">
        <div class="bar" style="flex-wrap:wrap;gap:8px">
          <input id="edName" value="${esc(d.name||'')}" style="width:110px" placeholder="姓名">
          <select id="edEdu" style="width:110px">
            ${['', '大专', '本科', '硕士', '博士'].map(v =>
              `<option value="${v}" ${(d.edu_level||'')===v?'selected':''}>${v||'（学历未识别）'}</option>`).join('')}
          </select>
          <input id="edSchool" value="${esc(d.school||'')}" style="width:160px" placeholder="学校">
          <input id="edMajor" value="${esc(d.major||'')}" style="width:160px" placeholder="专业">
          <select id="edGender" style="width:104px" title="简历没写就由你补填（已提取的会自动选中）；不参与档位判定">
            ${['', '男', '女'].map(v =>
              `<option value="${v}" ${((d.gender||'').trim())===v?'selected':''}>${v||'性别未填'}</option>`).join('')}
          </select>
        </div>
        <div class="bar" style="flex-wrap:wrap;gap:8px;margin-top:8px">
          <input id="edPhone" value="${esc(contactValue(d.phone)||'')}" style="width:160px" placeholder="电话">
          <input id="edEmail" value="${esc(contactValue(d.email)||'')}" style="width:220px" placeholder="邮箱">
        </div>
        <div class="small" style="margin-top:8px">识别错 / 识别不出时在这里直接改——改完点
          <b>右上角「保存更正」</b>；改前改后写入审计（电话 / 邮箱加密存储）；
          <b>技能与年限不在此处改</b>（必须来自简历原文与证据核对）</div>
      </div>
      ${(()=>{
        /* v1.25.6：信息缺失只在**真的有问题**时出现（说清"缺什么 + 下一步"，
           用户要求：说明缺失的信息是什么，而不是只提示缺失）；
           一切正常时不占版面（"没有重点有点乱"的另一半解法）。 */
        if (d.parse_failed) return `<div class="k">信息缺失</div>
          <div style="background:var(--bad-soft);border-radius:6px;padding:6px 10px">
            <b class="bad-txt">简历原文读不出</b>
            <span class="small">——去下方「简历附件」打开/下载原件人工核对，或重新导入一份可解析的文件</span></div>`;
        const mf = d.missing_fields || [];
        if (!mf.length) return '';
        const act = { '姓名': '在上方编辑区补填即可',
                      '年限': '以简历原文为准（核对后点「完整重新分析」重抽）',
                      '技能': '以简历原文为准（重新分析时按原文重建）' };
        const tips = [...new Set(mf.map(k=>act[k]).filter(Boolean))];
        return `<div class="k">信息缺失</div>
          <div style="background:var(--warn-soft);border-radius:6px;padding:6px 10px">
            <b style="color:var(--warn)">缺：${mf.map(esc).join('、')}</b>
            <span class="small">${tips.length?('——'+esc(tips.join('；'))):''}</span></div>`;
      })()}
      <div class="k">学历核验</div><div>${eduBadge(d)}${expBadge(d) ? (' · ' + expBadge(d)) : ''}</div>
      ${(()=>{
        // v1.17.1：紧跟院校/专业——荣誉、奖学金、论文、专利是**基础画像**的一部分，
        // 放在档案最底部等于藏起来。展示的是**简历原文那一行**（逐字来自原文，可核对）。
        const H = d.honors || {};
        const kinds = [['奖学金','奖学金'],['荣誉','荣誉'],['论文','论文'],['专利','专利']];
        const groups = kinds.map(([k,label])=>[label, (H[k]||[])]).filter(([,a])=>a.length);
        if (!groups.length) return '';
        return `<div class="k">荣誉 / 论文 / 专利</div><div>
          ${groups.map(([label,arr])=>`<div style="margin-top:4px">
            <span class="small" style="color:var(--ink-3)">${label}</span>
            ${arr.map(h=>`<div class="ev" style="margin:2px 0">${esc(h.evidence||h.name||label)}</div>`).join('')}
          </div>`).join('')}
          <div class="small" style="margin-top:4px">按简历原文规则识别，未逐条核实，请以原件为准</div>
        </div>`;
      })()}
      <div class="k">联系方式</div><div>${(()=>{
        const p=contactValue(d.phone), m=contactValue(d.email);
        if(!p && !m) return '<span class="small">未识别到联系方式（简历里可能确实没写）</span>';
        return `<span class="contact">${p?`<a href="tel:${esc(p)}">${esc(p)}</a>`:''}
          ${p&&m?' · ':''}${m?`<a href="mailto:${esc(m)}">${esc(m)}</a>`:''}</span>
          <button class="mini" onclick="copyText('${esc((p||'')+(p&&m?' / ':'')+(m||''))}')">复制</button>
          <span class="small">（库内加密存储）</span>`;
      })()}</div>
      <div class="k">信息复核</div><div>
        ${(()=>{
          // 复核 = "人事确认这个人的信息已核对完毕"（与档位无关：改档位不改变复核状态）
          const _a = (d.applications||[])[0] || {};
          // 注意：已确认时 status_display 返回空串（不打扰），
          // 所以这里必须读**原始** status 字段，不能拿 status_display 判断。
          const _done = (_a.status === '已确认');
          return `<b>${_done ? '已复核' : '未复核'}</b>
            <button class="btn-primary" onclick="markReview(${_a.id||'null'},${_done},${cid})">
              ${_done ? '撤销复核' : '我已核对过这个人的信息'}</button>
            <span class="small">复核只表示"看过并认可"，**不会改动档位**；
              不同意就直接改档位或拒绝。</span>`;
        })()}</div>
      <div class="k">所属岗位</div><div>
        ${(()=>{
          // 完整档案里直接给「修改岗位」：识别错、或人换了方向，都要能纠正归属。
          // v1.13.3 起后端支持改岗位（原来已归岗的会被 400 拒掉，HR 无处可改）。
          const _a = (d.applications||[])[0] || {};
          const _t = _a.job_title || '待指定';
          const _label = (_a.job_id ? '修改岗位' : '指定岗位');
          return `<b>${esc(_t)}</b>
            <button onclick="assignJobPick(${d.id},'pickjobD-${d.id}',${_a.id||'null'})">${_label}</button>
            <span class="small">改岗位后会按新岗位的 JD 重算建议档位，动作写入审计</span>`;
        })()}</div>
      <div class="k">档案状态</div><div>${esc(d.pool_status||'—')} · 密级标记 ${esc(d.pii_level||'普通')}
        · 投递 ${(d.applications||[]).length} 次 · 附件 ${(d.documents||[]).length} 份
        ${(function(){
          /* v1.25：已发过邮件的**打一个显眼标记**。
             为什么必须显眼：同一个人被两个 HR 各发一封面试邀请，
             是最难解释的一类事故。标记放在档案状态这一行、紧挨着"投递次数"，
             一进来就能看到，不用点进审计去找。 */
          const e = d.email_state || {};
          if (!e.sent) return '';
          const when = String(e.last_at||'').replace('T',' ').slice(0,16);
          return ` · <b style="color:var(--ok)">已发过邮件</b>`
            + `<span class="small">（共 ${e.count} 次${when?('，最近 '+esc(when)):''}`
            + `${e.last_subject?('，「'+esc(e.last_subject)+'」'):''}）</span>`;
        })()}</div>
    </div>
    ${dup}
    <div class="spacer"></div><div style="font-weight:600;font-size:15px">技能（均带原文证据）</div>
    <div style="margin-top:6px">${skills||'<span class="small">未识别到已核验技能</span>'}</div>
    <div style="margin-top:8px">${evs}</div>
    <div class="spacer"></div><div style="font-weight:600;font-size:15px">投递记录</div>
    <table><thead><tr><th>投递</th><th>岗位</th><th>渠道</th><th>投递时间</th><th>档位</th>
      <th>阶段</th><th>状态</th></tr></thead><tbody>${apps||'<tr><td colspan="7">—</td></tr>'}</tbody></table>
    <div id="pickjobD-${d.id}"></div>
    <div class="spacer"></div><div style="font-weight:600;font-size:15px">简历附件（原件留档，可下载）</div>
    <table><thead><tr><th>文件</th><th>解析</th><th>结果</th><th>接收时间</th><th>归档位置</th><th>操作</th></tr></thead>
      <tbody>${docs||'<tr><td colspan="6">—</td></tr>'}</tbody></table>
    <div class="spacer"></div><div style="font-weight:600;font-size:15px">操作审计
      <span class="small">只列确实改动了数据的操作；单纯"看了一下"的不在这儿
      （本地日志里都留着，需要时可调出）</span></div>
    <table><thead><tr><th style="width:170px">时间</th><th>做了什么</th><th style="width:90px">操作人</th>
      </tr></thead>
      <tbody>${(d.audit_snippet||[]).map(a=>`<tr>
        <td class="small">${esc(a.ts)}</td>
        <td>${esc(a.readable || a.action_label || a.action)}${
          a.entity === 'mail' ? ' <b style="color:var(--ok)">（邮件）</b>' : ''}</td>
        <td>${esc(a.operator)}</td></tr>`).join('')||'<tr><td colspan="3">还没有改动记录</td></tr>'}
      </tbody></table>
    <div class="spacer"></div><div style="font-weight:600;font-size:15px">简历原文</div>
    ${extractModeLine(d)}
    ${sensitiveLine(d.sensitive_removed)}
    <pre>${esc(d.raw_text||'（无文本，需人工查看附件原件；原件已留档）')}</pre>
    <div class="bar" style="margin-top:10px">
      <button data-reanalyze="${d.id}" onclick="reanalyze(${d.id},true)"
        title="重新解析原件 → 模型重抽字段 → 重建技能 → 重新归岗 → 重判建议档 → 重算分析。约 10 秒。">完整重新分析</button>
      <span class="small">抽取方式是「规则通道」时用它重跑一遍，就能换成模型抽取
        （不用重新导入，也不会多出一条投递）。HR 已定的档位/阶段/岗位不受影响，
        手动更正过的字段会保留。</span>
    </div>
    <div id="out-${d.id}"></div>`;
  document.getElementById('modal').classList.add('on');
}

/* 字段抽取方式（v1.23.4）：模型抽取 vs 规则降级。
   为什么单列一行：两者的字段质量差一档（技能靠词表扫 vs 模型读懂上下文），
   但界面上完全看不出来 —— 本项目就出过"模型静默降级、HR 以为分析过了"的事故。
   抽取方式取自 applications.extract_mode：
     llm+rule = 模型抽取成功（规则做兜底合并）
     heuristic = 规则通道（模型不可用/未启用）
     parse_failed = 没解析出文本 */
function extractModeLine(d){
  const apps = d.applications || [];
  const app = apps[0] || {};
  const m = app.extract_mode;
  if (!m){
    return `<div class="note" style="margin-bottom:6px">字段抽取方式：<b>未记录</b>
      （历史数据，早于本字段上线）。</div>`;
  }
  if (m === 'llm+rule'){
    return `<div class="note" style="margin-bottom:6px;background:var(--accent-soft)">
      字段抽取方式：<b>模型 + 规则</b>　技能与荣誉都要求能在原文定位到证据，定位不到的不计入。</div>`;
  }
  if (m === 'heuristic'){
    return `<div class="note" style="margin-bottom:6px;background:var(--warn-soft)">
      字段抽取方式：<b>规则通道（模型当时不可用）</b>　
      这份档案的字段是词表匹配 + 正则抽出来的，<b>质量低于模型抽取</b>。
      想重来：在「模型设置」确认模型可用后，重新导入这份简历即可。</div>`;
  }
  return `<div class="note" style="margin-bottom:6px">字段抽取方式：<b>${esc(m)}</b></div>`;
}

/* 送进模型前剔除了哪些敏感信息（v1.23.4）。
   为什么必须显示：这是隐私红线唯一能被**核查**的地方。
   原来界面上完全看不到，HR 只能选择相信；而且分母（有没有剔除）和
   分子（剔了什么）都不透明——"一条都没剔除"和"未记录"是两回事，
   得分清楚，不能糊成一句"已脱敏"。 */
function sensitiveLine(removed){
  const r = removed || {};
  const keys = Object.keys(r);
  if (!keys.length){
    return `<div class="note" style="margin-bottom:6px">模型看到的内容：本份简历未记录剔除明细
      （可能是历史数据）。<b>不是"没有敏感信息"</b>，只是没留痕。</div>`;
  }
  const parts = keys.map(k => `${esc(k)} <b>${r[k]}</b> 处`).join('、');
  return `<div class="note" style="margin-bottom:6px;background:var(--ok-soft)">
    <b>模型看到的内容已剔除：</b>${parts}
    <div class="small" style="margin-top:3px">下面是<b>原件解析出的完整原文</b>（含上述敏感信息，供人工核对）；
      送进模型的是剔除后的版本。剔除发生在调模型之前，且不改动原件。</div></div>`;
}
/* v1.25.6：弹层标题 + 头部动作按钮的统一入口。
   为什么统一：弹层是共用的（详情/进度/确认/上传都用一个 #modal），
   "保存更正"这类跟随弹层的按钮如果只由 showDetail 注入而别处不清，
   会残留在下一个弹层上。所有改标题的地方都走这里，头部动作就
   "随该弹层出现、随下一个弹层消失"。 */
function setModalTitle(title, headHtml){
  document.getElementById('mTitle').textContent = title;
  document.getElementById('mHeadBtn').innerHTML = headHtml || '';
}
function closeModal(){
  document.getElementById('modal').classList.remove('on');
  // v1.24.11：关闭后按需接续下一个动作（如收取完成 → 自动分析进度）
  const f = __afterClose; __afterClose = null;
  if (f){ try{ f(); }catch(e){} }
}

/* 人工更正档案字段：识别错/识别不出的兜底口子。
   姓名错会连带污染去重与检索；学校/专业错会让"专业方向判定"跟着错。
   与其让算法硬猜，不如让最了解情况的 HR 一步改对（后端写审计，改前改后留痕）。
   技能与年限**不在这里改**——那两项必须来自简历原文并通过证据核对。 */
async function saveFields(cid){
  const payload = {
    name: (document.getElementById('edName').value || '').trim(),
    education: document.getElementById('edEdu').value,
    school: (document.getElementById('edSchool').value || '').trim(),
    major: (document.getElementById('edMajor').value || '').trim(),
    gender: document.getElementById('edGender').value,
    phone: (document.getElementById('edPhone').value || '').trim(),
    email: (document.getElementById('edEmail').value || '').trim(),
  };
  const r = await api('/api/candidates/'+cid+'/rename',
                      {method:'POST', body:JSON.stringify(payload)});
  if (r.__http_error || r.error){
    toast(r.detail||r.error||'更正失败','danger'); return;
  }
  // v1.24.21：保存成功后**留在弹层原地刷新**，不要关掉。
  // 用户的诉求：「点了保存更正，明明成功了却报错，页面也不自动刷新」。
  // 原来的做法是 closeModal() —— 弹层一关，HR 看不到改后的值，也没法接着改下一项；
  // 而且只要后端返回非 2xx（如审计那步抛 500），这里就直接 return，
  // 弹层既不关也不刷新，看起来像"点了没反应"。
  // 现在：重新拉一次详情把弹层内容换成最新的，HR 能当场核对。
  toast(r.note || '已更正', 'ok');
  await showDetail(cid);
  refresh();          // 背后的列表/卡片也同步（改完姓名后列表要跟着变）
}

/* 页面内确认弹窗：替代浏览器原生 await askConfirm()。
   为什么换掉原生：它的样式、字号、按钮文案都不可控，不同浏览器长得不一样，
   而且和整站的设计语言完全割裂——用户看到的是"网页弹出的框"，不是"这个系统的框"。
   复用详情弹层的容器与样式，视觉一致；返回 Promise<boolean>，调用方 `await` 使用。

   入参可以是字符串（当正文）或 {title, body, okText, cancelText, danger}。
   `body` 允许含 HTML（调用方自行转义用户数据），换行按原样保留。 */
function askConfirm(opts){
  const o = (typeof opts === 'string') ? {body: opts} : (opts || {});
  return new Promise(resolve => {
    setModalTitle(o.title || '请确认');
    document.getElementById('mBody').innerHTML =
      `<div style="line-height:1.8;white-space:pre-wrap">${o.body || ''}</div>
       <div class="bar" style="margin-top:18px;justify-content:flex-end">
         <button onclick="__askResolve(false)">${esc(o.cancelText || '取消')}</button>
         <button class="${o.danger ? 'btn-danger' : 'btn-primary'}"
                 onclick="__askResolve(true)">${esc(o.okText || '确定')}</button>
       </div>`;
    window.__askResolve = v => {
      window.__askResolve = null;
      closeModal();
      resolve(v);
    };
    document.getElementById('modal').classList.add('on');
  });
}

// v1.6：统一说明"这次分析/提纲/解释用的是哪个岗位的尺子"。
// 不写清楚，HR 会默认它还是材料类那把默认尺子——沟通成本全在这里。
function jobLine(job, what){
  if (!job || !job.title) return '';
  const tag = job.source==='suggested' ? '建议岗位（尚未归岗，采纳后转为已归岗）'
            : job.source==='assigned'  ? '已归岗'
            : job.source==='explicit'  ? '指定的岗位' : '';
  return `<div class="small" style="margin-bottom:6px">${esc(what)}针对岗位：<b>${esc(job.title)}</b>`
       + (tag?(' · '+esc(tag)):'') + '</div>';
}
// v1.6：专业大类对照——回答"方向对不对口"，而不只是"缺哪几项技能"。
// 错配用红色：它是"换岗位才有意义"的方向问题，不是"培养可以补"的深度问题。
// v1.7：结论旁边必须带**置信度**与**通道**。
//   为什么：改造前"对口/无法判定"完全由"本体收没收这个词"决定——财务岗 9 项需求只收录 1 项，
//   两侧各剩同一个残留项、交集恰好非空，就判了"对口"。结论看起来正常，其实是巧合。
//   现在把"依据强弱"和"靠什么判的"一起摆出来，HR 才知道该信几分。
function majorBlock(mm){
  if (!mm || !mm.verdict) return '';
  const color = mm.verdict==='错配' ? 'var(--bad)' : (mm.verdict==='对口' ? 'var(--ok)' : 'var(--warn)');
  const list = c => Object.entries(c||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>k+'（'+v+'）').join('、')||'—';
  const conf = mm.confidence || '';
  const confColor = conf==='高' ? 'var(--ok)' : (conf==='中' ? 'var(--warn)' : 'var(--bad)');
  const chName = {大类:'按技能大类', 专业:'按专业维度', 词面:'按文字比对', 无:'无可用依据'}[mm.channel] || mm.channel || '';
  const mc = mm.major_check || {};
  const inList = mc.in_list === true ? '<b style="color:var(--ok)">在清单内</b>'
               : mc.in_list === false ? '<b style="color:var(--bad)">不在清单内</b>'
               : '<b style="color:var(--ink-3)">未识别</b>';
  const un = mm.unclassified || {};
  return `<div style="margin-top:8px;padding-top:8px;border-top:1px dashed #ddd">
    <b style="color:${color}">专业方向匹配：${esc(mm.verdict)}</b>`
    + (conf?`<span class="chip" style="margin-left:6px;color:${confColor};background:${conf==='高'?'var(--ok-soft)':(conf==='中'?'var(--warn-soft)':'var(--bad-soft)')}">置信度 ${esc(conf)}</span>`:'')
    + (chName?`<span class="small">（${esc(chName)}）</span>`:'')
    + (mm.major_label?`<span class="small">｜专业：${esc(mm.major_label)}</span>`:'')
    + `<br><span class="small">岗位侧重 ${esc(list(mm.job_categories))}｜候选人技能 ${esc(list(mm.cand_categories))}</span>`
    + ((un.job||un.cand)?`<br><span class="small" style="color:var(--ink-3)">本体未收录：岗位侧 ${un.job||0} 项、候选人侧 ${un.cand||0} 项——它不计入「侧重」统计，但已进入文字比对通道</span>`:'')
    + ((mc.required||[]).length?`<br><span class="small">专业需求：${esc((mc.required||[]).join('、'))} → 候选人专业 ${inList}</span>`:'')
    + (mc.via && mc.via !== '规则' ? `<br><span class="small" style="color:var(--accent-ink)">专业已由模型归一到学科目录（来源：${esc(mc.via)}），按归一结果判定，可复核</span>` : '')
    + `<br><span class="small">${esc(mm.note)}</span></div>`;
}
async function interview(cid){
  const el = document.getElementById('out-'+cid);
  el.innerHTML = '<div class="note">生成面试提纲中…</div>';
  const r = await api('/api/candidates/'+cid+'/interview', {method:'POST', body:JSON.stringify({focus:''})});
  if (r.error){
    el.innerHTML = (r.need_job)
      ? needJobHint(cid, '生成面试提纲')
      : '<div class="warn">'+esc(r.error)+(r.hint?('<br>'+esc(r.hint)):'')+'</div>';
    return;
  }
  el.innerHTML = `<div class="why" style="background:var(--surface-2);padding:12px;border-radius:var(--r-ctl);margin-top:10px">
    ${jobLine(r.job,'面试提纲')}
    <b>面试提纲（模型生成，供参考）</b>${(r.questions||[]).map((q,i)=>
      `<div style="margin-top:6px"><b>${i+1}. ${esc(q.q)}</b>
       <div class="small">考察：${esc(q.why||'')}</div></div>`).join('')}
    <div class="small" style="margin-top:8px">提纲按「该候选人对应岗位」的必需技能与职责生成；
      未归岗的投递建议先归岗或采纳建议岗位，否则题目会缺少针对性。</div></div>`;
}
async function doUpload(files){
  const arr = Array.from(files||[]).filter(f =>
    /[.](jpe?g|png|bmp|webp|pdf|docx?|txt)$/i.test(f.name));
  if (!arr.length){ resultModal({title:'上传', kind:'warn', body:'没有可上传的文件（支持图片 / PDF / Word / txt）'}); return; }
  // v1.24.20：原本只弹一个 toast 就开始传。
  // toast 3.5 秒自动消失，而多份文件的裸字节上传常常要十几秒——
  // 之后界面就完全没反馈了，和"导入弹窗不动"是同一类毛病。
  // 现在给出**可数的**进度（第几份/共几份），上传完自动换成结果弹窗。
  setModalTitle('上传简历文件');
  document.getElementById('mBody').innerHTML =
    `<div>
       <div style="font-size:14px;color:var(--ink-2);margin-bottom:4px">
         正在上传 ${arr.length} 份文件</div>
       <div class="progtrack"><div class="progfill" id="upFill" style="width:0%"></div></div>
       <div class="progmeta" id="upMeta">准备中…</div>
     </div>
     <div class="bar" style="margin-top:18px;justify-content:flex-end">
       <button class="btn-primary" onclick="closeModal()">关掉（后台继续传）</button>
     </div>`;
  document.getElementById('modal').classList.add('on');
  let ok = 0, fail = 0;
  const failedNames = [];
  const fill = document.getElementById('upFill');
  const meta = document.getElementById('upMeta');
  for (let i = 0; i < arr.length; i++){
    const f = arr[i];
    if (meta) meta.textContent = `第 ${i + 1} / ${arr.length} 份：${f.name}`;
    if (fill) fill.style.width = Math.round(i / arr.length * 100) + '%';
    try{
      // 裸字节上传：文件名走 query。不引 multipart 依赖（打包产物里没有它）
      const r = await fetch('/api/upload?name=' + encodeURIComponent(f.name),
        {method:'POST', headers:{'Content-Type':'application/octet-stream'}, body:f});
      const j = await r.json();
      if (r.ok && !j.error){ ok++; } else { fail++; failedNames.push(f.name + '：' + (j.error||'上传失败')); }
    }catch(e){ fail++; failedNames.push(f.name + '：网络错误'); }
  }
  // 完成提示：成功/部分失败/全部失败 三种状态分开说，并列出失败文件
  resultModal({
    title: '上传完成',
    kind: fail ? (ok ? 'warn' : 'danger') : 'ok',
    headline: fail ? (ok ? '部分文件未上传成功' : '全部文件上传失败') : '上传成功',
    body: `共 ${arr.length} 份：成功 ${ok} 份` + (fail ? `，失败 ${fail} 份` : '，全部已入库')
      + (failedNames.length
          ? `<div style="margin-top:10px;color:var(--bad);font-size:13px">失败明细：<br>`
            + failedNames.map(n => esc(n)).join('<br>') + '</div>'
          : '')
  });
  await refresh();
}

/* 收取/导入进行中标记（v1.24.13）。状态变量 __ingesting 已声明在 refresh() 之前。
   为什么必须有：邮箱收取和文件夹导入都是**几十秒级**的耗时操作，
   而按钮点下去到界面有反应之间有网络往返，HR 很容易以为没点上而连点多次。
   重复收取不只是"多跑一遍"——会并发抢同一个邮件游标、重复触发入库即分析，
   白白烧模型额度。所以：**一按下去就锁住，跑完（哪怕失败）才解锁**。 */

/* 进行中的"开始"提示。
   ⚠️ v1.24.20 改：进度条原本是**硬编码的 width:35%**，永远不动 ——
   HR 的原话是"这个弹窗不会变化，也不会根据导入结果自动退出"。
   接口实测只要 2 秒就返回，但一条死进度条 + 没有数字，
   在几十秒级的真实导入里就会被当成"卡住了"。

   现在改成**真在动的等待态**：
   · 宽度用 CSS 动画做不确定进度循环（不确定总耗时，不能假装有百分比）；
   · 旁边挂一个秒表，让"还在跑"变成可验证的事实而不是猜；
   · 超过 20 秒提示可能是在等模型，让 HR 知道没死。 */
function ingestStartModal(label){
  setModalTitle(label + '进行中');
  document.getElementById('mBody').innerHTML =
    `<div>
       <div style="font-size:14px;color:var(--ink-2);margin-bottom:4px">
         正在${label === '邮箱' ? '收取邮箱里的简历' : '读取本地文件夹里的简历'}，请稍候…</div>
       <div class="progtrack"><div class="progfill progfill--indet"></div></div>
       <div class="progmeta" id="ingestElapsed">已等待 <span id="ingestSecs">0</span> 秒，请勿重复点击</div>
       <div class="progmeta" id="ingestSlow" style="display:none;color:var(--warn,#b06a00)">
         耗时偏长，通常是在等模型分析。若超过 2 分钟仍无结果，可能是模型接口不通。</div>
     </div>
     <div class="bar" style="margin-top:18px;justify-content:flex-end">
       <button class="btn-primary" onclick="closeModal()">关掉（后台继续跑）</button>
     </div>`;
  document.getElementById('modal').classList.add('on');
  // 秒表：证明"还活着"。无论从哪条路出去，收尾都要停掉它。
  if (__ingestTick){ clearInterval(__ingestTick); __ingestTick = null; }
  let sec = 0;
  const secsEl = document.getElementById('ingestSecs');
  const slowEl = document.getElementById('ingestSlow');
  __ingestTick = setInterval(() => {
    sec += 1;
    if (secsEl) secsEl.textContent = sec;
    if (slowEl && sec === 20) slowEl.style.display = '';
  }, 1000);
}
function ingestStopTick(){
  if (__ingestTick){ clearInterval(__ingestTick); __ingestTick = null; }
}

/* 锁/解锁两个导入按钮。改 textContent 而不是 disabled，
   因为要看清"它到底在跑哪个"——disabled 的灰按钮在深色下容易读不出状态。 */
function setIngestBusy(busy, source){
  __ingesting = busy ? (source || '?') : null;
  // 解锁即收尾：秒表必须停，否则它会一直跑到下一个导入才被清掉。
  if (!busy) ingestStopTick();
  const cfg = [
    ['btnIngestMailbox', 'mailbox', '收取邮箱简历'],
    ['btnIngestFolder', 'folder',   '导入本地文件夹'],
  ];
  cfg.forEach(([id, src, text]) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (busy){
      el.disabled = true;
      el.textContent = (__ingesting === src) ? (text + '…进行中') : (text + '（等待中）');
      el.style.opacity = '.6';
      el.style.cursor = 'not-allowed';
    } else {
      el.disabled = false;
      el.textContent = text;
      el.style.opacity = '';
      el.style.cursor = '';
    }
  });
}

async function doIngest(source){
  const label = source==='mailbox' ? '邮箱' : '本地文件夹';
  // 已在跑就直接拒绝：重复收取会并发抢同一个邮件游标、
  // 重复触发入库即分析（每人都要调一次模型），代价是真金白银。
  if (__ingesting){
    toast(`已有一个导入任务在进行中（${__ingesting==='mailbox'?'邮箱收取':'文件夹导入'}），请等它完成`, 'warn');
    return;
  }
  setIngestBusy(true, source);
  ingestStartModal(label);
  let r;
  try{
    r = await api('/api/ingest', {method:'POST', body:JSON.stringify({source:source})});
  }catch(e){
    // 网络层异常也必须解锁，否则按钮永久卡死
    setIngestBusy(false);
    resultModal({title: label + '导入失败', kind:'danger', body: '请求失败：' + (e && e.message || e)});
    return;
  }
  if (r.__http_error || r.error){
    setIngestBusy(false);
    resultModal({title: label + '导入失败', kind:'danger', body: r.detail||r.error||'导入失败'}); return;
  }
  const num = k => (r[k]==null?0:r[k]);
  const ix = r.index || {};
  // 入库结果用居中弹窗报告（成功/失败一眼可见），不再顶部 toast
  const summary =
    `新增 ${num('added')}，新版本 ${num('merged_versions')}，` +
    `跳过重复 ${num('skipped_dup')}，无附件 ${num('no_attachment')}，` +
    `解析失败(仍入库) ${num('parse_failed')}，超限跳过 ${num('skipped_oversize')}，` +
    `异常 ${num('failed')}`;
  const hasProblem = (r.failed || r.skipped_oversize || num('insight_skipped') || r.empty_hint);
  const warnNote = num('insight_skipped') > 0
    ? `<br><br>有 ${num('insight_skipped')} 人未分析（"入库即分析"当前是关的）——可在上方点「分析待分析的人」批量补上。`
    : '';
  // v1.24.20：扫到 0 个文件时，必须说清是"文件夹是空的"还是"放错位置了"。
  // 只报"新增 0"的话，HR 无从判断——这正是刚才那个弹窗让人摸不着头脑的另一半。
  const emptyNote = r.empty_hint
    ? `<br><br><span style="color:var(--warn,#b06a00)">${r.empty_hint}</span>`
    : '';
  // 收尾了才解锁：入库即分析还在后台跑，但收取动作本身已经结束，
  // 允许 HR 排队下一次（避免"等模型分析"期间什么都做不了）。
  setIngestBusy(false);
  LAST_INGEST = r;                       // 明细留在「导入与来源」页看
  await refresh();
  // 入库后自动分析在后台跑（v1.24.9 接进度窗，v1.24.11 改为**收取结果确认后再开**）
  // 原因：progressModal 与 resultModal 共用 #modal，同时调用会把「收取完成」覆盖掉，
  // 用户反馈"没有完成提示"就是因为只看到了后面的进度条。
  const startInsight = () => {
    if (!r.insight_task_id) return;
    progressModal(r.insight_task_id, {
      title: '入库后自动分析',
      desc: `正在对新增的 ${num('insight_queued')} 人做模型分析`,
      onDone: () => refresh()
    });
  };
  const insightNote = r.insight_task_id
    ? `<br><br>接下来自动对新增的 ${num('insight_queued')} 人做模型分析，
       点「知道了」后开始跟踪进度。`
    : '';
  resultModal({
    title: label + '导入完成',
    kind: hasProblem ? 'warn' : 'ok',
    headline: hasProblem ? '有需要留意的地方' : (r.empty_hint ? '没有可导入的简历' : '导入成功'),
    body: `${summary}<br><span style="color:var(--ink-2)">源：${r.source_label||label}</span>` +
          emptyNote + warnNote + insightNote,
    afterClose: startInsight
  });
}
async function exportCsv(){
  // 导出内容按试用反馈定：**个人简介 + 对应岗位**，不掺内部判定口径。
  // 这份 CSV 是拿去用的（发给用人部门、贴进汇报、做面试排期），
  // 档位/命中/推荐理由属于系统内部判断，HR 要看在界面里看即可。
  // v1.7.1 起人才库分页展示——**导出必须拿全量**，不能只导当前页：
  // 另发一次不带 page 参数的请求（接口默认返回全量，兼容口径保留着），
  // 筛选条件（档位/关键词/性别）与当前列表保持一致。
  const full = await api('/api/candidates?tier=' + encodeURIComponent(TAB)
    + '&kw=' + encodeURIComponent(KW) + '&gender=' + encodeURIComponent(GENDER)
    + '&education=' + encodeURIComponent(EDU_MIN) + '&univ=' + encodeURIComponent(UNIV));
  const list = (full && full.items) || ITEMS;
  const head = ['姓名','性别','学历','院校','专业','专业方向/技能概要','手机','邮箱',
                '对应岗位','投递渠道','投递时间'];   // 校招口径：不导工作年限
  const rows = list.map(x=>{
    const sug = x.job_suggestion || null;
    const job = x.job_title || (sug ? `建议：${sug.title}` : '待指定');
    const skills = (x.skills||[]).slice(0,8).join('、');
    // 年限列在校招模式下不存在，行数据必须同步少一项——否则后面所有列整体错位
    return [x.name||'', x.gender||'', x.edu_level||'',
            x.school||'', x.major||'', skills,
            contactValue(x.phone), contactValue(x.email),
            job, x.channel||'', (x.applied_at||'').slice(0,10)];
  });
  const csv = [head].concat(rows).map(r=>r.map(v=>'"'+String(v).replace(/"/g,'""')+'"').join(',')).join('\\n');
  const blob = new Blob(['\\ufeff'+csv], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = '人才简介清单.csv'; a.click();
  toast(`已导出 CSV（${rows.length} 人，姓名/联系方式/技能概要 + 对应岗位）`,'ok');
}

/* --------------------- 投递管道（嵌入人才库，v1.7.5 折叠 + 完整看板） ---------------------
   独立「投递管道」导航页整体退出（VIEWS 已移除，#pipe 旧链接自动落回人才库）。
   形态按试用反馈定稿（v1.7.5）：**默认折叠成一行**，只看各阶段人数（超期红字）；
   点「展开」出完整看板——在招流程的阶段列全展示、每阶段人员全部列出（后端无截断）、
   行内带**阶段推进下拉**（原页的流程操作不丢），点姓名直接打开该人的完整档案。
   已入职 / 已结束是终态，不占看板（也不占折叠行），要看去候选人卡片上按阶段看。
   数据仍是 /api/pipeline 一份；折叠态记在 PIPE_OPEN，refresh 重渲染后不丢。 */
const PIPE_PER_COL = 15;   // 每列最多显示多少人（v1.15）：看板是"看积压"，不是"浏览全部"
const _PIPE_STAGES = STAGES.filter(s=>s!=='已入职' && s!=='已结束');   // 在招流程阶段
function pipeExpand(stage){
  PIPE_ALL[stage] = !PIPE_ALL[stage];
  refresh();
}
function goStageFilter(stage){
  STAGE_F = stage; TAB = 'ALL'; POOL_PAGE = 0;
  refresh();
  toast('已按阶段「' + stage + '」筛选人才库', 'ok');
}

function pipeToggle(){ PIPE_OPEN = !PIPE_OPEN; refresh(); }
function pipeBoardHtml(p){
  const order = (p && p.stage_order) || STAGES;
  const st = (p && p.stages) || {};
  const openTotal = (p && p.open_total) || 0;
  // 折叠行：在招流程各阶段人数，一段一行放下（超期红字提醒），已入职/已结束不计
  const counts = _PIPE_STAGES.map(s=>{
    const v = st[s] || {count:0, overdue:0};
    return `<span style="color:${v.overdue?'var(--bad)':'var(--accent-ink)'}"
      title="${esc(s)}：${v.count} 人${v.overdue?('，超期 '+v.overdue):''}">${esc(s)} ${v.count}${v.overdue?('（超期 '+v.overdue+'）'):''}</span>`;
  }).join('<span style="color:var(--line-2)"> · </span>');
  const head = `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <b>投递管道</b>
      <span class="small" style="color:var(--ink-2)">在流程中 ${openTotal} 条</span>
      <span style="flex:1"></span>
      <button id="pipeBtn" class="${PIPE_OPEN?'':'btn-primary'}" onclick="pipeToggle()">${PIPE_OPEN?'收起 ▲':'展开 ▼'}</button>
    </div>
    <div class="bar" style="margin-top:8px;flex-wrap:wrap;gap:6px 10px">${counts || '<span class="small" style="color:var(--ink-3)">各阶段暂无人</span>'}</div>`;
  if (!PIPE_OPEN) return `<div class="card" id="pipeCard" style="margin-bottom:12px">${head}</div>`;
  const chans = Object.entries((p && p.channels) || {})
    .sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${esc(k)} ${v}`).join(' · ');
  const cols = _PIPE_STAGES.map(s=>{
    const v = st[s] || {count:0, items:[], overdue:0};
    // 每条压成**一行**（v1.15）：几百人时每条占 3 行根本没法看。
    // 姓名 · 岗位 · 天数（超期红） · 行内推进下拉——下拉是常用动作，不能砍。
    // v1.16.1：超出 15 人时给**两个真出口**——就地展开，或跳人才库按阶段筛。
    // 原来只有一行灰字提示，而人才库当时并没有阶段筛选器，等于给了个死路。
    const _all = v.items||[];
    const _expandAll = !!PIPE_ALL[s];
    const _shown = _expandAll ? _all : _all.slice(0, PIPE_PER_COL);
    const _more = _all.length - _shown.length;
    const rows = _shown.map(i=>`
      <div class="it pipe-it" style="color:var(--ink-2)">
        <span class="pipe-nm" title="点击打开完整档案"
              onclick="showDetail(${i.candidate_id})">${esc(i.candidate_name||'未识别')}</span>
        ${i.job_title?`<span class="pipe-job">${esc(i.job_title)}</span>`:''}
        <span class="pipe-day${i.days>=15?' is-over':''}">${i.days}天</span>
        ${i.application_id?`<select class="pipe-sel" onchange="setStage(${i.application_id},this.value)"
          title="推进到所选阶段（写入审计）">
          ${STAGES.map(k=>`<option value="${k}" ${k===s?'selected':''}>${k}</option>`).join('')}
        </select>`:''}
      </div>`).join('') || '<div class="it">—</div>'
      + (_more>0
          ? `<div class="it pipe-more">还有 ${_more} 人 ·
              <a href="javascript:void(0)" onclick="pipeExpand('${esc(s)}')"
                 title="就在这一列里把剩下的也显示出来">在本列展开</a> ·
              <a href="javascript:void(0)" onclick="goStageFilter('${esc(s)}')"
                 title="跳到人才库，只看「${esc(s)}」阶段的人">去人才库筛选</a></div>`
          : (_all.length>PIPE_PER_COL && _expandAll
              ? `<div class="it pipe-more">已展开全部 ${_all.length} 人 ·
                   <a href="javascript:void(0)" onclick="pipeExpand('${esc(s)}')">收起</a></div>`
              : ''));
    return `<div class="pcol"><div class="h"><span>${esc(s)}</span>
      <span style="color:${v.overdue?'var(--bad)':'var(--ink-3)'}">${v.count}${v.overdue?(' / 超期'+v.overdue):''}</span></div>
      ${rows}</div>`;
  }).join('');
  return `<div class="card" id="pipeCard" style="margin-bottom:12px">${head}
    <div class="cols" style="margin-top:10px">${cols}</div>
    <div class="small" style="color:var(--ink-3);margin-top:8px"
         title="点姓名打开完整档案；行内下拉可直接推进阶段（写审计）；停留超过 15 天标红">每列最多显示 ${PIPE_PER_COL} 人（超出可展开或去人才库按阶段筛）· 点姓名看档案 · 超 15 天标红</div>
  </div>`;
}

/* ------------------------------ 归档 ------------------------------ */
// 软归档：只从人才库与检索里隐藏，档案/投递/附件/审计原样保留，随时可恢复。
async function viewArchive(){
  const c = await api('/api/candidates?archived=1');
  const items = c.items || [];
  ARCH_ITEMS = items;// 批量彻底删除时用来显示姓名
  document.getElementById('view').innerHTML = `
  <div class="panel"><h2>归档</h2>
    <div class="note">共 <b>${items.length}</b> 份已归档档案。</div>
    <div class="bar" style="margin-top:10px">
      <button onclick="archiveBatch(null,false)">批量取消归档（勾选的人）</button>
      <span class="vdiv"></span>
      <button class="btn-danger" onclick="purgeSelected()">批量彻底删除（勾选的人）</button>
      <span class="vdiv"></span>
      ${(()=>{
        // 与人才库保持同一套控件（这里原来是数字输入框——打字容易错、还能输负数）
        const y = new Date().getFullYear();
        const ys = [];
        for (let v=2018; v<=2050; v++) ys.push(v);
        return `<div class="arch-bar" style="margin:6px 0">
          <span class="arch-label">按年归档</span>
          <select id="archYear" onchange="pickArchYear(this.value, 'archYear')">
            ${ys.map(v=>`<option value="${v}"${v===y?' selected':''}>${v}</option>`).join('')}
          </select>
          <span class="arch-suffix">年以前</span>
          <button class="btn-primary" onclick="archiveByYear('archYear')">
            归档 ${y} 年以前的投递</button>
        </div>`;
      })()}
      <span class="vdiv"></span>
      <span class="small">按最后一条投递的年份整批归档，适合"新一年开始、旧简历收起来"</span>
    </div></div>
  ${items.length ? items.map(x=>{
    const t = x.tier_effective || 'D';
    const [fg,bg] = TIER_COLOR[t] || TIER_COLOR.D;
    const am = x.archive || {};
    const left = am.days_left;
    const expiring = left!=null && left<=7;
    // ⚠️ v1.24.20 修：expired 原来**根本没声明**，只有下面第 2080 行在用它。
    // 于是只要归档记录带上了 days_left（= 有归档到期时间），
    // viewArchive() 就抛 ReferenceError，归档页整页空白——
    // 而且只在有到期时间时才崩，所以早先 4 条 days_left 全为 null 时看不出来。
    // 这正是 MEMORY.md 里"let/const 暂时性死区"的又一次：判据要覆盖**所有数据形状**。
    const expired = left!=null && left<=0;
    const leftTxt = left==null ? ''
      : (expired ? `<span class="chip" style="color:var(--bad);background:var(--bad-soft)">归档已到期</span>`
                 : `<span class="chip" style="color:${expiring?'var(--bad)':'var(--ink-2)'};background:${expiring?'var(--bad-soft)':'var(--surface-2)'}"
                      title="归档 ${esc(am.purge_at||'')} 到期后自动彻底删除">归档中（${esc((am.purge_at||'').slice(0,10))} 到期）</span>`);
    return `<div class="card" style="border-left-color:${fg}">
      <div class="row1">
        <input type="checkbox" class="archChk" value="${x.id}" style="margin-right:10px">
        <div class="avatar">${esc((x.name||'?').slice(0,1))}</div>
        <div style="flex:1;min-width:0">
          <div class="nm">${esc(x.name||'未识别')} ${leftTxt}</div>
          <div class="meta">${eduBadge(x)}${expBadge(x) ? (' · ' + expBadge(x)) : ''} ·
            ${esc(x.school||'—')} · 阶段 ${esc(x.stage||'新投递')} ·
            归档于 ${esc((x.archived_at||'').slice(0,16)||'—')}</div>
          <div class="meta" style="margin-top:2px"><b>联系方式</b>：${contactLine(x)}</div>
        </div>
        <div class="badge" style="color:${fg};background:${bg}"><b>${t}</b><span>${TIER_LABELS[t]}</span></div>
      </div>
      <div class="acts">
        <button onclick="showDetail(${x.id})">完整档案</button>
        <button class="btn-primary" onclick="archiveCandidate(${x.id},false)">取消归档</button>
        <span class="vdiv"></span>
        <button class="btn-danger" onclick="purgeOne(${x.id})">彻底删除</button>
      </div>
    </div>`;}).join('') : '<div class="card">暂无归档档案。在人才库卡片上点「归档」即可移入这里。</div>'}`;
}
function archSelected(){
  return Array.from(document.querySelectorAll('.archChk:checked')).map(x=>parseInt(x.value));
}
async function archiveBatch(ids, archived){
  const list = ids || archSelected();
  if (!list.length){ toast('先勾选要处理的人','warn'); return; }
  const verb = archived ? '归档' : '取消归档';
  if (!await askConfirm(`将${verb} ${list.length} 人。\\n\\n` + (archived
      ? '归档后在「归档」页可见，可随时取消归档或彻底删除。继续？'
      : '取消归档后立即恢复在人才库与检索中展示。继续？'))) return;
  const r = await api('/api/candidates/archive-batch', {method:'POST',
    body:JSON.stringify({ids:list, archived:archived})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||verb+'失败','danger'); return; }
  toast(`已${verb} ${r.changed} 人${r.skipped?`（跳过 ${r.skipped} 人）`:''}`,'ok');
  refresh();
}
// v1.22.1：页面上有多处归档年份（人才库、归档页），各自一个下拉、各自一份状态，
// 所以把"选中的年份"记在 select 自己身上（dataset），不再用一个全局变量——
// 两处互相串台是很典型的"改了这边那边也变"的怪 bug。
function pickArchYear(v, selId){
  const y = parseInt(v, 10);
  if (!y || y < 1900 || y > 2200) return;
  const sid = selId || 'poolArchYear';
  const sel = document.getElementById(sid);
  if (sel) sel.dataset.year = String(y);
  // 按钮文案跟着变（同一个 .arch-bar 里的按钮）
  const bar = sel && sel.closest ? sel.closest('.arch-bar') : null;
  const btn = bar ? bar.querySelector('button.btn-primary') : document.getElementById('btnArchByYear');
  if (btn) btn.textContent = '归档 ' + y + ' 年以前的投递';
}
function archYearOf(selId){
  const sel = document.getElementById(selId || 'poolArchYear');
  const y = sel && parseInt(sel.dataset.year || sel.value, 10);
  return (y && y > 1900) ? y : new Date().getFullYear();
}
async function archiveByYear(selId){
  const y = archYearOf(selId);
  if (!await askConfirm(`把「最后一条投递早于 ${y} 年」的档案整批归档（当前还在人才库里的）。\\n\\n`
      + `归档后在「归档」页可见，可随时取消归档或彻底删除。继续？`)) return;
  const r = await api('/api/candidates/archive-batch', {method:'POST',
    body:JSON.stringify({before_year:y, archived:true})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'批量归档失败','danger'); return; }
  toast(`已归档 ${r.changed} 人（按 ${y} 年以前筛选命中 ${r.picked_by_year} 人）`,'ok');
  refresh();
}
function archiveByYearFrom(elId){ return archiveByYear(elId); }
async function purgeOne(cid){
  if (!await askConfirm('彻底删除后**不可恢复**：档案、投递、附件记录都会删除，'
    + '原件会移入回收目录（需要时请先在磁盘上复制一份）。\\n\\n确定彻底删除？')) return;
  const r = await api('/api/candidates/'+cid+'/purge', {method:'POST'});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'彻底删除失败','danger'); return; }
  toast('已彻底删除（原件移入回收目录，审计已留痕）','ok');
  refresh();
}
async function purgeSelected(){
  const ids = archSelected();
  if (!ids.length){ toast('先勾选要删除的人','warn'); return; }
  const names = ARCH_ITEMS.filter(x=>ids.includes(x.id)).map(x=>x.name||'?');
  const shown = names.slice(0,12).join('、') + (names.length>12?' 等':'');
  if (!await askConfirm('即将彻底删除 <b>'+ids.length+'</b> 份已归档档案：'
    + '<br>'+esc(shown)
    + '<br><br>档案、投递、附件记录都会删除，原件移入回收目录，<b>不可恢复</b>。'
    + '<br><br>确定删除？')) return;
  let ok = 0, fail = [];
  for (const cid of ids){
    const r = await api('/api/candidates/'+cid+'/purge', {method:'POST'});
    if (r.__http_error || r.error) fail.push(r.detail||r.error||String(cid)); else ok++;
  }
  toast(`已彻底删除 ${ok} 人${fail.length?`，失败 ${fail.length} 人：${esc(fail[0])}`:''}`,
        fail.length?'warn':'ok');
  refresh();
}
async function archiveCandidate(cid, on){
  if (on && !await askConfirm({title:'归档该候选人？',
      body:'归档后将从人才库与检索中隐藏，<b>进行中的投递会一并置为「已结束」</b>；'
         + '档案、投递、附件全部保留，可在「归档」页随时恢复。',
      okText:'归档'})) return;
  const r = await api('/api/candidates/'+cid+(on?'/archive':'/unarchive'), {method:'POST'});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'操作失败','danger'); return; }
  toast(on ? (r.note || '已归档') : '已取消归档，恢复在人才库与检索中展示', 'ok');
  refresh();
}
// 归岗 / 改岗位（v1.13.3 起同一个入口）：把投递归到 HR 指定的岗位，并按该岗位 JD 重算建议档位。
// applicationId 用于"一个人有多条投递"时精确定位是哪一条（不传就取最新一条）。
let _pickJobApp = null;        // 当前正在处理的投递 id
let _pickJobAppTitle = null;   // 该投递的原岗位名（确认框里说清"从哪改到哪"）
async function assignJob(cid, jobId, title, applicationId){
  const aid = (applicationId === undefined) ? _pickJobApp : applicationId;
  const _was = _pickJobAppTitle ? ('（原岗位：' + _pickJobAppTitle + '）') : '';
  if (!await askConfirm('把该投递的归属岗位设为「' + title + '」' + _was + '？\\n\\n'
    + '归岗/改岗位后按该岗位的 JD 重算建议档位，动作写入审计。')) return;
  const r = await api('/api/candidates/'+cid+'/assign-job',
    {method:'POST', body:JSON.stringify({job_id:jobId, application_id:aid || null})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'归岗失败','danger'); return; }
  if (r.unchanged){ toast('该投递本来就属于这个岗位，未做改动','warn'); return; }
  toast((r.old_job_title ? ('已从「'+r.old_job_title+'」改为「'+title+'」')
                         : ('已归岗到「'+title+'」'))
        + (r.note?('（'+r.note+'）'):'（已按岗位 JD 重算）'), 'ok');
  _pickJobAppTitle = null;
  refresh();
}

/* 手动指定岗位（v1.13）：待指定投递的**兜底入口**。
   为什么必须有：原来卡片上的「采纳建议岗位」只在系统给出建议时才渲染，
   模型判断不出（或模型未启用）时 HR 就**没有任何入口**把投递归到岗位——
   只能看着"所属岗位待指定"干瞪眼（实测反馈：「没有入口啊」）。
   这个入口与建议无关：直接从在招岗位里挑一个。
   就地展开选择器而不是弹层：HR 还在看这个人的其他信息，弹层会挡住上下文。 */
async function assignJobPick(cid, targetId, applicationId){
  const tid = targetId || ('pickjob-' + cid);
  const holder = document.getElementById(tid);
  if (!holder) return;
  _pickJobApp = applicationId || null;
  _pickJobAppTitle = null;
  if (!applicationId){
    // 没指定哪条投递时，取该候选人最新一条的原岗位名（只为确认框里说清"从哪改到哪"）
    const _cur = (ITEMS||[]).filter(x => x.id === cid)[0];
    _pickJobAppTitle = (_cur && _cur.job_title) || null;
  }
  if (holder.innerHTML) { holder.innerHTML = ''; return; }   // 再点一次收起
  holder.innerHTML = '<div class="note" style="margin-top:6px">读取在招岗位…</div>';
  const r = await api('/api/jobs');
  if (r.__http_error || r.error){
    holder.innerHTML = '<div class="note" style="color:var(--bad)">读取岗位失败</div>';
    return;
  }
  const jobs = (r.items || []).filter(j => j.active !== 0);
  if (!jobs.length){
    holder.innerHTML = '<div class="note" style="color:var(--warn)">还没有在招岗位——'
      + '请先到「岗位管理」建一个岗位（含学历门槛与必需技能），再回来归岗。</div>';
    return;
  }
  const opts = jobs.map(j => '<option value="' + j.id + '">' + esc(j.title)
      + (j.department_name ? (' · ' + esc(j.department_name)) : '') + '</option>').join('');
  holder.innerHTML = '<div class="bar" style="flex-wrap:wrap;margin-top:6px">'
    + '<span class="small">归到岗位：</span>'
    + '<select id="pickjobSel-' + cid + '" style="width:220px">' + opts + '</select>'
    + '<button class="btn-primary" onclick="assignJobFromPick(' + cid + ')">确认归岗</button>'
    + '<button onclick="assignJobPickClear(this)">取消</button>'
    + '<span class="small">归岗后按该岗位的 JD 重算建议档位，动作写入审计</span>'
    + '</div>';
}
/* 取消：从按钮往上找 .bar，把它的父容器（就是 pickjob 挂载点）清空。
   不用在字符串里拼 id/引号——之前那样写转义容易被吞掉（踩过）。 */
function assignJobPickClear(btn){
  const bar = btn && btn.closest ? btn.closest('.bar') : null;
  if (bar && bar.parentElement) bar.parentElement.innerHTML = '';
}
async function assignJobFromPick(cid){
  const sel = document.getElementById('pickjobSel-' + cid);
  if (!sel) return;
  const jid = parseInt(sel.value, 10);
  const title = sel.options[sel.selectedIndex].textContent;
  await assignJob(cid, jid, title);
}

/* 判断建议岗位（v1.13.2）：**主动**让模型判断一次并落库。
   为什么要做成显式动作、而不是页面加载时自动算：模型判断一次 6-8 秒且烧 token，
   而"建议岗位"看一眼就够。原来列表接口每次打开人才库都重算一遍，
   HR 实测「每次点人才库都很慢、还浪费 token」。现在结论存库、展示时免费读取，
   想更新时点一下这个按钮即可（动作写审计）。 */
async function suggestJob(cid){
  const out = document.getElementById('out-'+cid);
  const btn = document.getElementById('sugjob-' + cid);
  if (btn){ btn.disabled = true; btn.textContent = '判断中…（约几秒）'; }
  try {
    const r = await api('/api/candidates/'+cid+'/suggest-job', {method:'POST'});
    if (r.__http_error || r.error){
      const msg = r.detail || r.error || '判断失败';
      toast(msg, 'danger');
      if (out) out.innerHTML = '<div class="note" style="color:var(--bad)">' + esc(msg) + '</div>';
      return;
    }
    toast(r.job_id ? ('建议岗位：' + r.title + (r.reason ? ('（' + r.reason + '）') : ''))
                   : r.note, r.job_id ? 'ok' : 'warn');
    refresh();
  } finally {
    if (btn){ btn.disabled = false; btn.textContent = '判断建议岗位'; }
  }
}
/* 未归岗时分析类功能会如实报错——但光报错没用，得给一个**能点的入口**。
   实测反馈：没有建议岗位时"无法分析也无法修改投递岗位"。 */
function needJobHint(cid, what){
  return '<div class="note" style="color:var(--warn)">该候选人还没有对应岗位（既未归岗、'
    + '也无建议岗位），' + esc(what) + '需要一个岗位当尺子。'
    + '<div class="bar" style="margin-top:6px">'
    + '<button class="btn-primary" onclick="needJobAssign(this,' + cid + ')">指定岗位</button>'
    + '<button onclick="suggestJob(' + cid + ')">判断建议岗位</button>'
    + '</div></div>';
}
/* 就地挂载选择器：档案页里已经有 #pickjobD-<cid> 挂载点就复用它，
   没有（例如从卡片弹出的提示里）就现造一个挂在按钮下面——避免出现两个同名 id。 */
function needJobAssign(btn, cid){
  let holder = document.getElementById('pickjobD-' + cid);
  if (!holder){
    holder = document.createElement('div');
    holder.id = 'pickjobD-' + cid;
    const wrap = btn.closest('.bar').parentElement;
    wrap.appendChild(holder);
  }
  holder.innerHTML = '';
  assignJobPick(cid, holder.id);
}
/* 「入库即分析」开关 + 手动批量补分析（v1.13.4）

   为什么要开关：入库即分析每次都会调模型（一次 2-8 秒、消耗额度）。
   有的人想"进门就有判断"，有的人想"先攒一批、月底一次性补"——两种都得支持，
   所以做成开关而不是二选一写死。

   手动批量是**循环调用**接口（每次 5 人）而不是一次性全量：
   模型一次 2-8 秒，一次性 30 人 = 几分钟黑屏等待，且 HTTP 容易超时；
   分批能显示"已分析 5/12"的进度，HR 看得见它在动。 */
async function setAutoInsight(on){
  const r = await api('/api/settings', {method:'POST',
    body:JSON.stringify({auto_insight_on_ingest: !!on})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'保存失败','danger'); return; }
  toast(r.note || '已保存','ok');
  refresh();
}
/* 批量完整重新分析（v1.23.9）：把「抽取方式=规则通道」的档案按当前配置重跑一遍。
   为什么需要：老档案是在模型不可用时入库的，抽取方式永远是规则通道；
   一个个点「重新分析」太慢。
   为什么分批循环：每人 6-15 秒（两次模型调用），一次全跑会超时且看不到进度。
   这与「分析待分析的人」是同一套交互口径：按钮变进度条 → 循环 → 结束刷新。 */
async function reanalyzeBatch(){
  const btn = document.getElementById('btnReanalyzeBatch');
  if (!btn || btn.disabled) return;
  const ok = await askConfirm({
    title: '批量按模型重跑？',
    body: `<div>将对<b>抽取方式还是「规则通道」</b>的档案，逐人完整重跑：<br><br>
      ① 重新解析原件 → ② 模型重抽字段 → ③ 重建技能 → ④ 重新归岗 → ⑤ 重判建议档<br><br>
      <b>不会动的：</b>HR 已定的档位、阶段、已归岗的岗位；<b>你手动更正过的字段会保留</b>。<br>
      <b>会变的：</b>模型抽出的字段与技能（技能可能变少——模型只认能在原文定位到证据的）。<br><br>
      每人约 6-15 秒，串行处理。可以随时关页面，已完成的不会白做。</div>`,
    okText: '开始重跑', danger: false});
  if (!ok) return;
  btn.disabled = true;
  let done = 0, fail = 0, taskId = null;
  try {
    for(;;){
      const r = await api('/api/candidates/reanalyze-batch',
        {method:'POST', body:JSON.stringify({limit: 5, only_heuristic: true})});
      if (r.__http_error || r.error){
        resultModal({title:'批量重跑失败', kind:'danger', body: r.detail || r.error || '批量重跑失败'}); break;
      }
      done += (r.analyzed || 0); fail += (r.failed || 0);
      taskId = r.task_id || taskId;
      const left = r.remaining || 0;
      if (!left) break;
      // 每批之间稍等，让后台串行分析往前走
      await new Promise(rs => setTimeout(rs, 300));
    }
  } finally {
    btn.disabled = false;
    refresh();
  }
  // 用居中弹窗报告最终结果（不再用顶部 toast）
  if (fail) resultModal({title:'批量重跑完成', kind:'warn',
    headline:'部分失败', body:`成功 ${done} 人，失败 ${fail} 人。<br>失败的点开档案看具体原因。`});
  else if (done) resultModal({title:'批量重跑完成', kind:'ok',
    headline:'全部完成', body:`${done} 人已改为模型抽取。`});
  else resultModal({title:'批量重跑', kind:'info', body:'没有需要重跑的人了。'});
}

async function analyzePendingBatch(){
  const btn = document.getElementById('btnAnalyzePending');
  if (!btn || btn.disabled) return;
  const total0 = parseInt((btn.dataset.pending || '0'), 10);
  btn.disabled = true;
  let done = 0, fail = 0, taskId = null;
  try {
    for(;;){
      const r = await api('/api/insights/analyze-pending',
        {method:'POST', body:JSON.stringify({limit: 5})});
      if (r.__http_error || r.error){
        resultModal({title:'分析失败', kind:'danger', body: r.detail || r.error || '分析失败'}); break;
      }
      done += (r.analyzed || 0); fail += (r.failed || 0);
      taskId = r.task_id || taskId;
      const left = r.remaining || 0;
      if (!left || !(r.analyzed || 0)) break;
      // 若还有剩余，用进度弹窗跟踪（后端返回了 task_id）
      if (left && taskId){
        progressModal(taskId, {title:'分析中', desc:`正在分析待分析的人（共 ${total0} 人）`,
          onDone: () => { refresh(); }});
        // 进度弹窗接管了，这里退出循环，交给弹窗轮询
        break;
      }
      await new Promise(rs => setTimeout(rs, 300));
    }
  } finally {
    btn.disabled = false;
    if (!taskId) refresh();
  }
  // 最终结果：有 taskId 时进度弹窗已展示结果；否则直接弹结果
  if (!taskId){
    if (fail) resultModal({title:'分析完成', kind:'warn', headline:'部分失败',
      body:`成功 ${done} 人，失败 ${fail} 人。`});
    else if (done) resultModal({title:'分析完成', kind:'ok', headline:'全部完成', body:`${done} 人已分析。`});
  }
}
/* 复核（v1.14.1）：认可系统这次给的建议档，**不改档位**。
   为什么单独一个动作：档位是 HR 的判断，复核是"我看过"。
   以前绑在一起（改档位 = 顺手置已确认），等于诱导 HR 为了消标签而改档位。 */
async function markReview(aid, undo, fromDetail){
  if (!aid) { toast('这条投递还没有记录','warn'); return; }
  if (!await askConfirm(undo ? '撤销复核？\\n\\n会回到"未复核"，系统不会改档位。'
    : '确认复核这条建议？\\n\\n表示你认可系统给的建议档（**档位不会被改动**），'
      + '以后模型再更新判断时会重新提示你。')) return;
  const r = await api('/api/applications/'+aid+'/review',
    {method:'POST', body:JSON.stringify({undo: !!undo})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'操作失败','danger'); return; }
  // 静默成功（Gate 16）：复核后档案就地刷新，用户看得见结果，不需要再弹一次提示
  _pickJobAppTitle = null;
  if (fromDetail){ try{ return void await showDetail(fromDetail); }catch(e){} }
  refresh();
}

/* ------------------------------ 智能助手 ------------------------------ */
async function viewChat(){
  const runs = await api('/api/agent/runs?limit=1');
  const t = META.tools || {};
  document.getElementById('view').innerHTML = `  <div class="panel chat-page">
    <div class="flexbetween">
      <div><h2 style="margin:0">HR 助手（智能体）</h2>
        <div class="note">写操作只生成待确认提案，点确认才生效。</div></div>
      <div class="small" style="text-align:right">
        工具 ${t.total||0} 个：读 ${(t.read||[]).length} / 算 ${(t.compute||[]).length}
        / 写(需确认) ${(t.write_requires_confirmation||[]).length}<br>
        禁用：${(t.disabled||[]).join('、')}<br>
        累计运行 ${(runs.cost||{}).runs||0} 次，平均 ${(runs.cost||{}).avg_latency_ms||0} ms
      </div>
    </div>
    <div id="chatRetention" class="note" style="display:none;margin-bottom:8px"></div>
    <div class="chatlog" id="chatLog"></div>
    <div class="bar chat-foot">
      <input id="chatInput" placeholder="输入问题或指令" style="flex:1;min-width:240px"
             onkeydown="if(event.key==='Enter'){askAgent()}">
      <button class="btn-primary" onclick="askAgent()">发送</button>
      <button id="chatRestoreBtn" onclick="restoreChat()" style="display:none"
              title="把刚清空的对话找回来。清空后 30 天内有效，到期系统自动清理底层记录">恢复对话</button>
      <button onclick="clearChat()" title="清空对话展示；30 天内可恢复，到期系统自动清理底层运行记录">清空</button>
    </div>
  </div>`;
  // v1.6：进页面就把已落库的对话拉回来（刷新/重启后不再"历史消失"）
  await loadChatHistory();
}
// 历史渲染：与实时推送共用 pushMsg，助手消息后面挂"模式/轮数/工具链"
async function loadChatHistory(){
  const log = document.getElementById('chatLog');
  if (!log) return;
  const h = await api('/api/agent/history');
  const msgs = h.messages || [];
  log.innerHTML = '';
  // v1.6「清空 = 软清空」：清空后 30 天内可恢复，界面必须如实说出这条线与到期时间，
  // 不能让人以为"点了清空就找不回来了"。
  renderChatRetention(h);
  if (!msgs.length){
    log.innerHTML = '<div class="note" style="padding:10px">'
      + (h.restorable
         ? `这批对话已清空，将在 <b>${esc(h.purge_after||'')}</b> 自动清理；`
           + '在此之前可点上方「恢复对话」找回。'
         : '还没有对话记录。历史会保留：刷新页面、重启服务后仍能看到；'
           + '<b>只有点「清空」才会清空</b>，且清空后 30 天内还能恢复。')
      + '</div>';
  }
  msgs.forEach(m=>{
    // 助手消息走 Markdown 渲染（v1.24.9），用户消息仍是纯文本
    const d = pushMsg(m.role, m.content, m.role === 'assistant');
    if (m.role !== 'assistant') return;
    const bits = [];
    if (m.mode) bits.push('模式：'+m.mode);
    if (m.rounds) bits.push('轮数 '+m.rounds);
    if (m.latency_ms) bits.push(m.latency_ms+' ms');
    if (m.tokens) bits.push('tokens '+m.tokens);
    if ((m.tools||[]).length) bits.push('工具：'+m.tools.map(x=>x.tool).join(' → '));
    if (bits.length) d.insertAdjacentHTML('beforeend',
      '<details class="tracebox"><summary>本次调用详情</summary>'
      + '<div class="trace">'+esc(bits.join(' · '))+'</div></details>');
    /* v1.25：历史回放时也把**卡片重建出来**。
       原先卡片只活在 DOM 里，切一次页面（refresh → viewChat → 本函数）就被抹掉，
       HR 想回看"我刚才让它做了什么"就没了。现在服务和历史里都带着卡片数据，
       这里按同样的渲染函数重建，刷新/换页/重启都还在。
       已处理过的提案显示状态（已执行/已拒绝）而不是按钮 —— 见 renderProposalCards。 */
    if ((m.drafts||[]).length) renderDraftCards(d, m.drafts);
    if ((m.proposals||[]).length) renderProposalCards(d, m.proposals);
  });
  // CHAT 从库里恢复 → 多轮追问的上下文连续（后端只取最近 8 轮）
  CHAT = msgs.map(m=>({role:m.role, content:m.content}));
  log.scrollTop = log.scrollHeight;
}
async function clearChat(){
  if (!await askConfirm('清空对话记录？\\n\\n界面上的历史会全部清空，此后从零开始。\\n'
             + '但不会立刻销毁：30 天内随时可以点「恢复对话」找回，\\n'
             + '到期（满 30 天）后系统会自动清理底层运行记录，那时才不可恢复。\\n'
             + '清空/恢复/清理三条痕迹永久保留在「提案与审计」中，便于复盘与核算。')) return;
  const r = await api('/api/agent/clear', {method:'POST'});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'清空失败','danger'); return; }
  CHAT = [];
  const log = document.getElementById('chatLog');
  if (log) log.innerHTML = '';
  toast(`已清空 ${r.cleared||0} 条对话；${r.retention_days||30} 天内可点「恢复对话」找回`
        + `（将于 ${r.purge_after||'—'} 自动清理）`,'ok');
  await loadChatHistory();
}
// v1.6：撤销清空——把刚清空的对话从库里找回来（保留期内）
async function restoreChat(){
  if (!await askConfirm('恢复刚清空的对话？\\n\\n界面上的历史会重新出现。\\n'
             + '注意：恢复后这批记录不再有到期时间，会一直保留到下次清空。')) return;
  const r = await api('/api/agent/restore', {method:'POST'});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'恢复失败','danger'); return; }
  if (!r.ok){ toast(r.note||'没有可恢复的对话','warn'); await loadChatHistory(); return; }
  toast(r.note || `已恢复 ${r.restored||0} 条对话`,'ok');
  await loadChatHistory();
}
// 保留期提示条：清空后显示"将于 X 自动清理、还剩 N 天"，并可一键恢复
function renderChatRetention(h){
  const ret = document.getElementById('chatRetention');
  const btn = document.getElementById('chatRestoreBtn');
  const on = !!h.restorable;
  if (btn) btn.style.display = on ? '' : 'none';
  if (!ret) return;
  if (!on){ ret.style.display = 'none'; ret.innerHTML = ''; return; }
  const left = (h.days_left === null || h.days_left === undefined)
    ? '' : `（还剩 <b>${h.days_left}</b> 天）`;
  ret.style.display = '';
  ret.innerHTML = `本批对话已于 <b>${esc(h.cleared_at||'')}</b> 清空，`
    + `将于 <b>${esc(h.purge_after||'')}</b> 自动清理${left}；`
    + `在此之前可点「恢复对话」找回，清理执行后即不可恢复。`
    + `清空 / 恢复 / 清理三条痕迹永久保留在「提案与审计」中。`;
}
function pushMsg(role, text, isMd){
  const log = document.getElementById('chatLog');
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  if (isMd){ d.classList.add('md'); d.innerHTML = md(text); }
  else { d.textContent = text; }
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  return d;
}
async function askAgent(){
  const inp = document.getElementById('chatInput');
  const q = (inp.value||'').trim(); if (!q) return;
  inp.value = ''; pushMsg('user', q);
  const holder = pushMsg('assistant', '思考中…');
  const r = await api('/api/agent/chat', {method:'POST',
    body:JSON.stringify({message:q, history:CHAT})});
  // Markdown 渲染（v1.24.9）：模型回复是 Markdown，textContent 会让 **/#/- 裸奔
  const answer = r.answer || (r.detail||'(无回复)');
  holder.classList.add('md');
  holder.innerHTML = md(answer);
  let extra = '';
  if (r.trace && r.trace.length){
    /* v1.25：工具轨迹**默认折叠**。
       原来是把 `→ search_candidates({...})` 一行行摊在回复下面——
       对 HR 是无意义噪音（他们不关心函数名和 JSON 参数），
       还会把真正要看的结论挤下去。收进「详情」里，需要排查时才展开。
       ⚠️ 有 [已拦截] 的时候**默认展开**：那是"这件事没做"的原因，
       属于必须让 HR 看见的信息，不能藏在折叠里。 */
    const blocked = r.trace.some(t => t.disabled);
    const lines = r.trace.map(t =>
      '→ ' + esc(t.tool_label || t.tool) + '(' + esc(JSON.stringify(t.args)) + ')'
      + (t.write ? ' <b>[写·需确认]</b>' : '')
      + (t.disabled ? ' <b>[已拦截]</b>' : '')
      + (t.recovered ? ' <b>[已补做]</b>' : '')).join('<br>');
    extra += '<details class="tracebox"' + (blocked ? ' open' : '') + '>'
      + '<summary>工具调用（' + r.trace.length + ' 次'
      + (blocked ? ' · 有被拦截的' : '') + '）</summary>'
      + '<div class="trace">' + lines + '</div></details>';
  }
  if (r.mode) extra += `<div class="trace">模式：${esc(r.mode)} · 轮数 ${r.rounds||0} · ${r.latency_ms||0} ms`
    + (r.tokens && r.tokens.total_tokens ? (' · tokens '+r.tokens.total_tokens) : '') + '</div>';
  if (extra) holder.insertAdjacentHTML('beforeend', extra);
  CHAT.push({role:'user',content:q}, {role:'assistant',content:r.answer||''});
  if ((r.drafts||[]).length) renderDraftCards(holder, r.drafts);
  if ((r.pending_proposals||[]).length) renderProposalCards(holder, r.pending_proposals);
}

/* 待确认提案：当场就能点确认/拒绝（v1.13.8）。
   为什么要这个：智能体写完提案只说"请到系统里确认"，多跳一步的结果就是被忽略
   （HR 实测反馈）。提案是**唯一允许 HR 点头才生效**的东西，
   所以点头的地方就该在消息下面，而不是另一个页面。
   权限：没有 confirm 权限的人不显示按钮（后端也会拒），只提示去看审计页。
   确认流只有一条：复用 POST /api/proposals/{pid}/decide，不新增写接口。 */
/* ⚠️ v1.25 修一个**长期存在、且一直没被发现**的 bug：
   这两个常量是 JS 字符串（"approve" / "reject"），但拼进 HTML 属性时
   **没有加引号**，生成出来是：
       onclick="decideProposal(37,approve,this)"      ← approve 是裸标识符
   浏览器执行时就把它当**变量**去找 → `ReferenceError: approve is not defined`
   → 点了完全没反应（既不弹确认框，也不报错给用户看）。

   为什么一直没被发现：`decideProposal` 本身没问题，
   用控制台直接调 `decideProposal(1,'approve')` 是好的；
   但**从界面上点按钮**走的是 HTML 属性求值那条路，一按就炸。
   —— 又一个「必须真点一次才算验证」的例子。

   修法：给常量加单引号（属性本身用双引号包着，所以内层只能用单引号）。
   用 JSON.stringify 会产出双引号，直接把 HTML 属性截断，不能用。 */
const APPROVE = String.fromCharCode(97,112,112,114,111,118,101);   // approve
const REJECT  = String.fromCharCode(114,101,106,101,99,116);       // reject
//: 拼进 onclick 属性时用的**带引号**形式
const APPROVE_Q = "'" + APPROVE + "'";
const REJECT_Q  = "'" + REJECT  + "'";
function renderProposalCards(holder, proposals){
  const perms = (META && META.session && META.session.permissions) || [];
  const canConfirm = perms.indexOf('confirm') >= 0;
  let html = '';
  proposals.forEach(p => {
    /* v1.24.18：加「打开档案」。原来点「确认执行」是**盲签**——
       HR 只看到一句"把陈志远推进到初面"，无法核对他确认的是不是这个人的档案。
       提案里的 candidate_id 缺失就不给这个按钮（宁可没有，也不要指错人）。 */
    const openBtn = p.candidate_id
      ? '<button onclick="showDetail(' + p.candidate_id + ')">打开档案核对</button>' : '';
    /* v1.25：**已处理过的提案**（历史回放时会出现）显示状态，不再给按钮。
       为什么：从历史里重建的卡片，如果还挂着「确认执行」，点下去后端会回
       "该提案已是『已执行』状态，无需重复处理" —— 看着像报错，其实是对的。
       与其让 HR 白点一次，不如直接把它标成已执行/已拒绝。 */
    const done = p.status && p.status !== '待确认';
    const acts = done
      ? '<span class="chip" style="color:'
          + (p.status === '已执行' ? 'var(--ok)' : 'var(--ink-3)') + ';background:'
          + (p.status === '已执行' ? 'var(--ok-soft)' : 'var(--surface-2)')
        + '">' + esc(p.status) + '</span>' + openBtn
      : (canConfirm
        ? '<button class="btn-primary" onclick="decideProposal(' + p.proposal_id + ',' + APPROVE_Q + ',this)">确认执行</button>'
          + '<button class="btn-danger" onclick="decideProposal(' + p.proposal_id + ',' + REJECT_Q + ',this)">拒绝</button>'
          + openBtn
        : '<span class="small">你没有「确认」权限，请到「提案与审计」页处理</span>' + openBtn);
    /* v1.25：把「改前 → 改后」摆出来，而不是只有一句摘要。
       为什么：HR 点「确认执行」是真改档案。原来只有 summary
       （"更正 赵敏 的字段：电话：… → …"）——一句中文里塞多个改动，
       人多改动多时读不出来，还得点「打开档案核对」跳去看。
       这里把后端已算好、本来只拼进 summary 的前后值**结构化列出**，
       在卡片上就能核对完。字段名与顺序都由后端给，前端不做任何推断。 */
    let detailHtml = '';
    const changes = p.changes || [];
    if (changes.length){
      detailHtml = '<div class="small" style="margin-top:8px">'
        + changes.map(c => '<div style="padding:2px 0">'
            + '<b>' + esc(c.label || c.field || '') + '</b>：'
            + '<span style="color:var(--ink-3)">' + esc(c.before || '（空）') + '</span>'
            + ' <span style="color:var(--ink-3)">→</span> '
            + '<span style="color:var(--accent-ink)">' + esc(c.after || '（空）') + '</span>'
          + '</div>').join('')
        + '</div>';
    }
    html += '<div class="card" style="background:var(--surface)8e6;margin-top:10px" id="prop-' + p.proposal_id + '">'
      + '<div style="font-weight:600">待确认提案 · ' + esc(p.tool_label || p.tool || '')
        + '<span class="small">提案 #' + p.proposal_id + (p.risk ? (' · 风险 ' + esc(p.risk)) : '') + '</span></div>'
      + '<div style="margin-top:6px">' + esc(p.summary || '') + '</div>'
      + detailHtml
      + '<div class="bar" style="margin-top:8px">' + acts
        + '<span class="small">不点就不会生效——系统不会替你做决定</span></div></div>';
  });
  holder.insertAdjacentHTML('beforeend', html);
}
async function decideProposal(pid, decision, btn){
  const card = document.getElementById('prop-' + pid);
  const _q = decision === 'approve' ? '确认执行这条提案吗？\\n\\n它会真的改档案（写审计）。'
                                 : '拒绝这条提案？\\n\\n拒绝也会写入审计（谁在什么时候拒的）。';
  if (!await askConfirm(_q)) return;
  if (btn) btn.disabled = true;
  const r = await api('/api/proposals/' + pid + '/decide',
    {method:'POST', body:JSON.stringify({decision: decision})});
  if (r.__http_error || r.error){
    toast(r.detail || r.error || '处理失败','danger');
    if (card){ card.querySelectorAll('button').forEach(x => x.disabled = false); }
    return;
  }
  if (card){
    // 采纳/驳回后的瞬时高亮沿用色板，不用随手写的十六进制：
    // 绿是 ok-soft（比 ok 淡，做背景刚好），驳回用中性 surface-2——
    // 驳回不是错误，只是一次选择，所以不给红色。
    card.style.background = decision === 'approve' ? 'var(--ok-soft)' : 'var(--surface-2)';
    card.innerHTML = '<div style="font-weight:600">'
      + (decision === 'approve' ? '已执行' : '已拒绝') + ' · 提案 #' + pid + '</div>'
      + '<div class="small" style="margin-top:4px">' + esc(r.note || '') + '</div>';
  }
  /* ⚠️ v1.25 修：原来这里无条件 `refresh()`，而 refresh() 会**重绘整个对话区**
     （viewChat() 重建 #view，聊天记录从 CHAT 数组重新拼，卡片不在 CHAT 里）。
     于是刚写好的「已执行」卡片**立刻被抹掉**——HR 点完确认，卡片凭空消失，
     根本不知道成没成功（实测：后端状态已是"已执行"，但界面显示"卡片已消失"）。
     现在：只在**非对话页**刷新（那里有列表/统计需要同步）；
     对话页上就地保留卡片，卡片自己已经变成"已执行"了。 */
  if (VIEW !== 'chat') refresh();
}

/* v1.25.6：待确认提案清单弹窗（统计卡"待确认提案"的落点）。
   与对话卡片同一套动作（复用 /api/proposals/{pid}/decide，不新增写接口）；
   每行带 id="prop-{pid}"，decideProposal 的"已执行/已拒绝"就地替换逻辑直接可用。
   注意 askConfirm 与列表共用同一个 #modal，所以确认/取消之后都要把列表重开。 */
async function openProposals(){
  const r = await api('/api/proposals?status=' + encodeURIComponent('待确认'));
  const items = (r && r.items) || [];
  setModalTitle('待确认提案（' + items.length + '）');
  document.getElementById('mBody').innerHTML = items.length
    ? `<div class="small" style="margin-bottom:8px">提案由智能体生成，逐条确认后才会真正落库。
        ${(r && r.can_confirm === false) ? '<b>你没有「确认」权限</b>' : ''}</div>`
      + items.map(p =>
        `<div class="card" id="prop-${p.id}" style="margin:8px 0;padding:10px 12px">
           <div style="font-weight:600">${esc(p.tool_label || p.tool || '')}
             <span class="small">提案 #${p.id}${p.risk ? (' · 风险 ' + esc(p.risk)) : ''}</span></div>
           <div style="margin-top:4px">${esc(p.summary || '')}</div>
           <div class="bar" style="margin-top:8px">
             <button class="btn-primary" onclick="decideProposalInModal(${p.id},'approve',this)">确认执行</button>
             <button onclick="decideProposalInModal(${p.id},'reject',this)">拒绝</button>
           </div>
         </div>`).join('')
    : '<div class="small">没有待确认的提案。</div>';
  document.getElementById('modal').classList.add('on');
}

/* 弹窗版提案处理：先确认、后提交；askConfirm 会顶掉本列表，所以结束后重开列表。 */
async function decideProposalInModal(pid, decision, btn){
  const q = decision === 'approve'
    ? '确认执行这条提案吗？\\n\\n它会真的改档案（写审计）。'
    : '拒绝这条提案？\\n\\n拒绝也会写入审计（谁在什么时候拒的）。';
  if (!await askConfirm(q)) { openProposals(); return; }
  if (btn) btn.disabled = true;
  const r = await api('/api/proposals/' + pid + '/decide',
    {method:'POST', body:JSON.stringify({decision: decision})});
  if (r.__http_error || r.error){
    toast(r.detail || r.error || '处理失败','danger');
    openProposals();
    return;
  }
  toast(decision === 'approve' ? '已执行（写入了审计）' : '已拒绝', 'ok');
  refresh();           // 页面统计 / 列表同步（#modal 不受 refresh 影响）
  openProposals();     // 重开列表：该提案已从"待确认"里消失
}

/* 智能体起草的邮件：渲染成卡片 + 一键送进邮件编辑器（v1.13.7）。
   为什么要有这个卡：智能体说"我无法发送邮件"时，HR 真正缺的是**能用的草稿**。
   这里把草稿原样亮出来，HR 可以直接复制，或点按钮进编辑器改完自己点发送——
   **智能体不发送，这是红线**。 */
/* v1.24.18：由 `let DRAFTS = []`（整表覆盖）改为**按卡片 id 存**。
   原实现每渲染一轮就 `DRAFTS = drafts` 整体覆盖，于是第二轮对话之后，
   第一轮那张草稿卡上的「确认并发送」会读到**第二轮的草稿**——
   收件人、正文全错，而按钮点下去是真发信。这不是显示问题，是会误发邮件。
   id 用递增序号，历史卡片永远指向自己那份数据。 */
let DRAFTS = {};
let _draftSeq = 0;
/* v1.25：从对话草稿卡点「在编辑器中修改」时，先把草稿挂到这个全局变量，
   再切到写邮件页。viewMail() 渲染完编辑器后会来取它填进正文——
   这样即使切页后再次 refresh（比如用户又点了一下导航），草稿也不会被冲掉。
   旧实现直接 `await viewMail()` 但不切 VIEW，结果 refresh 一触发就回到聊天页，
   HR 看到的就是"点过去啥也没有"。 */
let PENDING_DRAFT = null;
/* key 必须**全局递增**，不能每轮从 d0 重来。
   renderDraftCards 每轮都被调用一次，i 是本轮内的下标——
   若直接 `'d' + i`，第二轮的 d0 又会盖掉第一轮的 d0，历史卡片照样串。
   所以先领一个全局唯一号，再登记。 */
const draftKey = () => 'd' + (_draftSeq++);

/* 声明必须放在读取者之前：`let`/`const` 有暂时性死区，被后面代码读会抛
   ReferenceError → 整页白屏（本项目踩过，见 .workbuddy/memory 3.3）。 */
function renderDraftCards(holder, drafts){
  let html = '';
  drafts.forEach((d) => {
    const key = draftKey();           // 全局唯一号
    DRAFTS[key] = d;                  // 登记到全局表，历史卡片不会被覆盖
    const miss = (d.missing_runtime||[]).length
      ? `<div class="warn-txt" style="margin-top:6px">还缺：${esc((d.missing_runtime||[]).join('、'))}（补上后可以在对话里说"补上 XX 再写一版"）</div>` : '';
    const noMail = !d.to
      ? `<div class="bad-txt" style="margin-top:6px">库里没有这个人的邮箱，无法直接发——请先在档案里补邮箱，或改成你手动转发</div>` : '';
    /* v1.24.18：草稿卡补齐「直接发」与「打开档案」两个动作。
       原先只有「在邮件编辑器中打开」——那是**跳页**：HR 点一下被扔到邮件页，
       还要自己确认收件人、正文对不对，再点一次发送。从"助手已经起草好了"到
       "真的发出去"隔了两次跳转，实践里就断在这儿了。
       现在三个动作并排放在卡片里：发送（就地，带二次确认）/ 编辑 / 看档案。
       红线不变：按钮写的是「确认并发送」，且必须 HR 点，助手永不自动发。 */
    const acts =
      (d.to && !d.sent
        ? `<button class="btn-primary" onclick="sendDraftNow('${key}', this)">确认并发送</button>` : '')
      + `<button onclick="openDraftInMailerByKey('${key}')">在编辑器中修改</button>`
      + (d.candidate_id ? `<button onclick="showDetail(${d.candidate_id})">打开档案</button>` : '')
      /* ⚠️ v1.25 修：原来是 `copyText(JSON.stringify(''))` —— 复制的是一段**空字符串**，
         点了只弹"已复制主题与正文"，其实什么都没进剪贴板。
         改成在卡片上带一份真实的主题+正文，由 copyDraft 复制。 */
      + `<button onclick="copyDraft('${key}')">复制主题与正文</button>`;
    /* v1.25：已发过的草稿**不再给发送按钮**。
       历史重建时若无条件还原按钮，HR 切页后再点就会重复发信；
       这是"卡片常驻"带来的新风险，必须一起堵掉。 */
    const sentNote = d.sent
      ? `<div class="small" style="margin-top:6px;color:var(--ok)">这封已发送过（见下方状态）。`
        + `要再发一封，请点「在编辑器中修改」确认内容后发送。</div>`
      : '';
    html += `<div class="card" style="background:var(--surface-2);margin-top:10px" id="draft-${key}">
      <div style="font-weight:600">邮件草稿${d.sent ? '（已发送）' : '（未发送）'} · ${esc(d.name||'')}
        <span class="small">收件人：${esc(d.to || '（无邮箱）')}${d.job?(' · 岗位：'+esc(d.job)):''}`
      + `${d.template?( ' · 模板：'+esc(d.template)):''}</span></div>
      <div class="small" style="margin-top:6px">主题：${esc(d.subject||'')}</div>
      <div class="small" style="margin-top:6px;color:var(--ink-3)">正文预览：</div>
      <div class="mail-prev" style="margin-top:4px;border:1px solid #e3e6ea;border-radius:8px;
        padding:8px;max-height:260px;overflow:auto;background:#fff;font-size:13px">
        ${d.body_html || esc(d.body||'')}</div>
      ${miss}${noMail}${sentNote}
      <div class="bar" style="margin-top:8px">${acts}
        <span class="small">${!d.to ? '没有邮箱就发不出去'
          : (d.sent ? '已发送过，不再重复提供发送按钮' : '点「确认并发送」即寄出到上面这个地址')}</span>
      </div></div>`;
  });
  holder.insertAdjacentHTML('beforeend', html);
}
/* 按 key 取草稿并送进编辑器。单独包一层是为了让按钮只传字符串 key，
   不把整个草稿对象内联进onclick（正文可能很长，内联会撑爆 HTML 属性）。 */
function openDraftInMailerByKey(key){
  const d = (window.DRAFTS || DRAFTS || {})[key];
  if (!d) { toast('草稿已失效，请让助手重新起草','danger'); return; }
  return openDraftInMailer(null, d);
}

/* 从对话里就地发送草稿（v1.24.18）。
   为什么不用现成的 sendMailConfirm()：那套读的是邮件页表单里的收件人/正文，
   而草稿卡里的内容在对话里，两者不共享状态。硬套会把编辑器里被改过的内容覆盖掉。
   这里直接用草稿自己的字段，走同一个后端 /api/mail/send（单一真源，后端留痕一致）。 */
async function sendDraftNow(key, btn){
  const d = (window.DRAFTS || DRAFTS || {})[key];
  if (!d || !d.to) { toast('这封草稿没有收件人，无法发送','danger'); return; }
  const body = (d.body||'').trim();
  if (!body) { toast('草稿正文是空的，先补内容','danger'); return; }
  // 二次确认要把「发给谁、发的是什么」摆在眼前，不能只问一句"确定吗"
  if (!await askConfirm('把这封信寄给 ' + d.to + '（' + (d.name||'') + '）吗？\\n\\n'
      + '主题：' + (d.subject||'') + '\\n\\n寄出后不能撤回，发送记录会写进审计。'))
    return;
  if (btn) btn.disabled = true;
  const r = await api('/api/mail/send', {method:'POST', body:JSON.stringify({
    to: d.to, subject: d.subject||'', body: d.body_html||body,
    html: d.body_html||'', candidate_id: d.candidate_id, template_name:'智能助手草稿'})});
  const card = document.getElementById('draft-' + key);
  if (r.__http_error || r.error){
    toast(r.detail || r.error || '发送失败','danger');
    if (btn) btn.disabled = false;
    return;
  }
  delete DRAFTS[key];
  if (card){
    card.style.background = 'var(--ok-soft)';
    card.innerHTML = '<div style="font-weight:600">已发送 · ' + esc(d.name||'') + '</div>'
      + '<div class="small" style="margin-top:4px">收件人 ' + esc(d.to) + '｜发送记录已写入审计。</div>';
  }
  /* v1.25：与提案卡同一个 bug —— 对话页上 refresh() 会重绘整个对话区，
     把刚写好的「已发送」状态抹掉（HR 点完看不到结果）。
     现在只在非对话页刷新；对话页就地保留卡片。
     （历史重建时靠审计查出的 sent 标记恢复成"已发送"，见 db.chat_history） */
  if (VIEW !== 'chat') refresh();
}

async function openDraftInMailer(idx, d){
  if (!d || (!d.body && !d.body_html)){
    toast('这封草稿没有正文，请让助手重新起草','danger'); return;
  }
  // v1.25：先挂全局，再切到写邮件页（VIEW='mail' + 重新渲染 viewMail）。
  // ⚠️ v1.25.x 修「跳转过去是空白」：原来用 `location.hash='mail'` 切页，
  // 它会**异步**触发 hashchange 事件，其处理函数 go('mail',true) 会再 render 一遍 viewMail——
  // 第二次渲染发生在 PENDING_DRAFT 已被第一次 viewMail 消费清空之后，
  // 于是刚填好的正文被冲掉，表现就是"点『在编辑器中修改』跳转过去是空白"。
  // 改用 history.replaceState 改 hash：**不触发 hashchange**，于是只有一次渲染，正文留存。
  PENDING_DRAFT = d;
  closeModal();
  VIEW = 'mail';
  if (location.hash !== '#mail'){
    try { history.replaceState(null, '', '#mail'); } catch(e){}
  }
  renderTabs();
  try {
    // 唯一的一次渲染：viewMail 渲染完编辑器后会读 PENDING_DRAFT 自动填正文。
    await refresh();
  } catch (e) {
    // viewMail 万一抛错也要尽量把草稿填进去，而不是静默失败。
    console.error('openDraftInMailer refresh 失败', e);
  }
  // 兜底再填一次：即便极端时序下 viewMail 没消费 PENDING_DRAFT，这里也能保证填好。
  applyDraftToMailer(d);
  toast('已填入编辑器：确认内容后由你点「确认发送」','ok');
}

/* v1.25：把草稿填进写邮件页的控件（抽出来，viewMail 与 openDraftInMailer 共用）。
   优先用 body_html（模板渲染结果 / free-write 的 to_html 结果，带 <p>/<br>，换行正确）；
   没有 body_html 才回落到转义后的纯文本 body。 */
function applyDraftToMailer(d){
  if (!d) return;
  try {
    const sel = document.getElementById('mCand');
    if (sel && d.candidate_id != null) sel.value = String(d.candidate_id);
    // 智能助手起草的草稿里带着收件人地址（v1.24.9）。
    // 直接用草稿里的 to 字段填，并走 setMailTo() 醒目显示（邮箱错一位就是发错人）。
    if (d.to) setMailTo(d.to, d.name || '');
    const inp = document.getElementById('mCandInput');
    if (inp) inp.value = d.name || (d.to ? String(d.to) : '');
    const sub = document.getElementById('mSubject');
    if (sub) sub.value = d.subject || '';
    const ed = document.getElementById('mailEditor');
    if (ed) ed.innerHTML = d.body_html || esc(d.body || '');
    const pv = document.getElementById('mailPreview');
    if (pv && d.body_html) pv.innerHTML = d.body_html;
  } catch (e) { toast('填充编辑器失败：' + e, 'danger'); }
}

/* ------------------------------ 导入与来源 ------------------------------ */
// 回答三个问题：从哪儿导？导入了什么？结果怎么样？
/* 简历来源与导入配置（原「导入与来源」页，已并入「系统配置」）。
   导入动作本身在人才库页有按钮，这里只放配置：来源目录、邮箱、历史记录。 */
async function renderImportCfgInto(boxId){
  const box = document.getElementById(boxId);
  if (!box) return;
  const s = await api('/api/sources');
  const f = s.folder || {}, last = LAST_INGEST || s.last_ingest || null;
  const rec = s.recycle || {};
  const files = f.files || [];
  const lim = f.max_attachment_mb==null?20:f.max_attachment_mb;
  const fileRows = files.map(x=>`<tr>
      <td><input type="checkbox" class="fileChk" data-name="${esc(x.name)}"
           data-doc="${x.document_id||''}"></td>
      <td>${esc(x.name)}${x.over_limit
          ? `<span class="chip" style="color:var(--bad);background:var(--bad-soft)">超 ${lim}MB，导入会跳过</span>`:''}</td>
      <td>${fmtSize(x.size)}</td>
      <td>${esc(x.mtime||'')}</td>
      <td>${x.indexed?'<span class="ok-txt">已入库</span>'+(x.candidate?('（'+esc(x.candidate)+'）'):'')
                    :'<span class="warn-txt">尚未入库</span>'}</td>
      <td class="nw">
        <button onclick="openSourceFile('${esc(x.name)}',1)">在线预览</button>
        <button onclick="downloadSourceFile('${esc(x.name)}')">下载</button>
        <button class="btn-danger" onclick="removeSourceFiles(['${esc(x.name)}'])">删除</button>
      </td></tr>`).join('');
  box.innerHTML = `
  <div class="panel">
    <h2 style="margin:0">导入与来源</h2>
  </div>

  <div class="card"><h2>① 来源一：本地文件夹</h2>
    <div class="bar" style="margin-top:8px">
      <span class="small">文件夹</span>
      <input id="srcDir" value="${esc(f.config_dir||'')}" style="width:420px"
             placeholder="data/resumes 或 /Users/you/简历">
      <button class="btn-primary" onclick="saveSourceDir()">保存路径</button>
      <button onclick="refresh()">刷新清单</button>
    </div>
    <div class="srcbox" style="margin-top:8px">当前扫描目录：<code>${esc(f.path||'（未配置）')}</code>
      ${f.exists?'':'<span class="bad-txt">（目录不存在）</span>'}
      ｜发现 ${files.length} 份可导入的简历
      ｜单个文件上限 <b>${lim} MB</b>（超大文件导入时跳过，不会被删）
    </div>
    <div class="bar" style="margin-top:12px">
      <span class="small">执行导入的按钮在「人才库」页 —— 配置在这里，动作在那边，避免两处都能点。</span>
      <span class="vdiv"></span>
      <label style="display:flex;align-items:center;gap:5px"><input type="checkbox" id="chkAll"
        onchange="toggleAllFiles(this.checked)"> 全选</label>
      <button onclick="bundleDownload()">批量下载选中（<span id="selCount">0</span>）</button>
      <button class="btn-danger" onclick="removeSelected()">批量删除选中（<span id="selCount2">0</span>）</button>
    </div>
    <table style="margin-top:10px"><thead><tr>
      <th style="width:34px"></th><th>文件</th><th>大小</th><th>修改时间</th><th>入库状态</th><th class="nw">操作</th>
    </tr></thead>
      <tbody>${fileRows||'<tr><td colspan="6">目录里还没有可导入的简历文件</td></tr>'}</tbody></table>
    ${(rec.count||0) ? `<div class="srcbox" style="margin-top:12px">
      <div class="flexbetween"><div>回收目录（已「删除」的简历仍在，共 <b>${rec.count}</b> 份）
        <span class="small">— ${esc(rec.dir||'')}</span></div>
        <button onclick="showRecycle()">查看 / 恢复</button></div>
      <div class="small" style="margin-top:4px">最近一次：${esc(((rec.last_remove||{}).at)||'—')}
        ${((rec.last_remove||{}).moved||[]).length?('，移入 '+((rec.last_remove||{}).moved||[]).map(m=>esc(m.name)).join('、')):''}</div>
    </div>` : ''}
  </div>

  <div class="card"><h2>② 最近一次导入：导入了什么、结果如何</h2>
    ${last ? ingestDetailHtml(last) : '<div class="note">还没有导入记录。去「人才库」页点「收取邮箱简历」或「导入本地文件夹」执行一次即可。</div>'}
  </div>`;
  hookFileChecks();
}
async function removeSelected(){
  const sel = checkedFiles();
  if (!sel.length){ toast('请先勾选要删除的简历','warn'); return; }
  removeSourceFiles(sel.map(x=>x.name));
}
async function removeSourceFiles(names){
  // 二次确认：一次性把一批简历移出来源目录，点错一次要逐个恢复，代价不对称。
  const list = names.slice(0,5).join('、') + (names.length>5 ? (' 等 '+names.length+' 份') : '');
  if (!await askConfirm('将把以下文件移入回收目录（可恢复，不是物理删除）：\\n\\n'+list+'\\n\\n继续？')) return;
  const r = await api('/api/sources/remove', {method:'POST', body:JSON.stringify({names:names})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'删除失败','danger'); return; }
  const moved = (r.moved||[]).length, skipped = (r.skipped||[]).length;
  const why = (r.skipped||[]).slice(0,2).map(x=>x.name+'：'+x.why).join('；');
  toast('已移入回收目录 '+moved+' 份'+(skipped?('；'+skipped+' 份未处理（'+why+'）'):'')
        +'。可到「查看 / 恢复」取回。', skipped?'warn':'ok');
  refresh();
}
async function showRecycle(){
  const r = await api('/api/sources/removed');
  setModalTitle('回收目录 · ' + (r.recycle_dir||''));
  const rows = (r.items||[]).map(x=>`<tr>
    <td>${esc(x.name)}</td><td class="nw">${fmtSize(x.size)}</td>
    <td class="nw">${esc(x.removed_at)}</td>
    <td class="nw">${x.conflict?'<span class="warn-txt">来源目录已有同名文件</span>':'—'}</td>
    <td class="nw"><button onclick="restoreFiles(['${esc(x.name)}'])">恢复到来源目录</button></td></tr>`).join('');
  document.getElementById('mBody').innerHTML = `
    <div class="note">${esc(r.note||'')}</div>
    <div class="spacer"></div>
    <table><thead><tr><th>文件</th><th>大小</th><th>删除时间</th><th>同名冲突</th><th>操作</th></tr></thead>
      <tbody>${rows||'<tr><td colspan="5">回收目录是空的</td></tr>'}</tbody></table>
    <div class="bar" style="margin-top:12px">
      <button class="btn-primary" onclick="restoreAll()">全部恢复</button>
      <button onclick="closeModal()">关闭</button></div>`;
  document.getElementById('modal').classList.add('on');
  window.__recycle = (r.items||[]).map(x=>x.name);
}
async function restoreFiles(names){
  const r = await api('/api/sources/restore', {method:'POST', body:JSON.stringify({names:names})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'恢复失败','danger'); return; }
  const ok = (r.restored||[]).length, sk = (r.skipped||[]).length;
  const why = (r.skipped||[]).slice(0,2).map(x=>x.name+'：'+x.why).join('；');
  toast('已恢复 '+ok+' 份'+(sk?('；'+sk+' 份未恢复（'+why+'）'):''), sk?'warn':'ok');
  closeModal(); refresh();
}
function restoreAll(){
  const all = window.__recycle || [];
  if (!all.length){ toast('回收目录是空的','warn'); return; }
  restoreFiles(all);
}
function fmtSize(n){
  const v = Number(n||0);
  if (v < 1024) return v + ' B';
  if (v < 1024*1024) return (v/1024).toFixed(1) + ' KB';
  return (v/1024/1024).toFixed(2) + ' MB';
}
function hookFileChecks(){
  const boxes = document.querySelectorAll('.fileChk');
  boxes.forEach(b => b.addEventListener('change', updateSelCount));
  updateSelCount();
}
function checkedFiles(){
  return Array.from(document.querySelectorAll('.fileChk:checked'))
    .map(b => ({name:b.getAttribute('data-name'), document_id:b.getAttribute('data-doc')||null}));
}
function updateSelCount(){
  const n = String(checkedFiles().length);
  ['selCount','selCount2'].forEach(id=>{
    const el = document.getElementById(id);
    if (el) el.textContent = n;
  });
}
function toggleAllFiles(on){
  document.querySelectorAll('.fileChk').forEach(b => { b.checked = !!on; });
  updateSelCount();
}
async function saveSourceDir(){
  const dir = document.getElementById('srcDir').value.trim();
  const r = await api('/api/mailbox/config',{method:'POST',body:JSON.stringify({folder_dir:dir})});
  if (r.__http_error||r.error){ toast(r.detail||r.error||'保存失败','danger'); return; }
  toast(dir?('简历来源文件夹已改为：'+dir):'已恢复为默认目录','ok');
  refresh();
}
function ingestDetailHtml(r){
  const n = k => (r[k]==null?0:r[k]);
  const ix = r.index || {};
  const rows = (r.details||[]).map(d=>{
    const st = {'added':'<span class="ok-txt">新增</span>',
                'merged_version':'<span class="ok-txt">新版本归档</span>',
                'skipped_dup':'<span class="warn-txt">重复跳过</span>',
                'skipped_oversize':'<span class="bad-txt">超限跳过</span>',
                'failed':'<span class="bad-txt">异常</span>'}[d.status] || esc(d.status||'—');
    return `<tr><td>${esc(d.file||'—')}</td><td>${st}</td>
      <td>${esc(d.name||'—')}</td><td>${esc(d.tier||'待分析')}</td>
      <td class="small">${esc((d.notes||[]).join('；')||'')}</td></tr>`;
  }).join('');
  const mailRows = (r.mail_details||[]).map(m=>`<tr><td>${esc(m.subject||'')}</td>
      <td class="small">${esc(m.from||'')}</td><td>${esc(m.result||'')}</td></tr>`).join('');
  return `<div class="srcbox">来源：<b>${esc(r.source_label||r.source||'—')}</b>
      ｜时间 ${esc(r.at||'（本次会话）')}｜操作人 ${esc(r.operator||'hr')}</div>
    <div class="bar" style="margin:10px 0">
      新增 <b class="ok-txt">${n('added')}</b>　新版本 <b>${n('merged_versions')}</b>　
      重复跳过 <b class="warn-txt">${n('skipped_dup')}</b>　无附件 ${n('no_attachment')}　
      解析失败(仍入库) ${n('parse_failed')}　
      超限跳过 <b class="bad-txt">${n('skipped_oversize')}</b>　异常 <b class="bad-txt">${n('failed')}</b>
      ${r.routed==null?'':`　标题归岗 <b>${n('routed')}</b>　待指定 ${n('unassigned')}`}
    </div>
    <div class="srcbox">自动建索引：新增 <b>${n('indexed')}</b> 条，跳过 ${n('skipped')} 条
      ${ix.error?`<span class="warn-txt">（${esc(ix.error)}）</span>`:''}
      <span class="small">（增量：画像没变的人不会重新算）</span></div>
    ${mailRows?`<div style="margin-top:12px;font-weight:600">邮件处理</div>
      <table><thead><tr><th>主题</th><th>发件人</th><th>结果</th></tr></thead><tbody>${mailRows}</tbody></table>`:''}
    <div style="margin-top:12px;font-weight:600">逐份简历结果</div>
    <table><thead><tr><th>文件</th><th>结果</th><th>姓名</th><th>档位</th><th>说明</th></tr></thead>
      <tbody>${rows||'<tr><td colspan="6">本次没有产生逐份明细（例如邮箱里没有新邮件）</td></tr>'}</tbody></table>`;
}
async function previewMail(){
  const out = document.getElementById('mailPreview');
  out.innerHTML = '<div class="note">正在连接邮箱读取邮件列表（只读，不下载正文）…</div>';
  const r = await api('/api/mailbox/preview?limit=10', {method:'POST', body:JSON.stringify({})});
  if (r.__http_error || r.ok===false){
    out.innerHTML = `<div class="warn">连接失败：${esc(r.error||r.detail||'未知错误')}
      <div class="small">请到「邮箱配置」核对服务器、账号与授权码（口令不回显，留空则沿用已保存的）。</div></div>`;
    return;
  }
  out.innerHTML = `<div class="srcbox">${esc(r.message||'')}
      账号 <b>${esc(r.account||'—')}</b>｜收件夹 ${esc(r.folder||'')}</div>
    <table style="margin-top:8px"><thead><tr><th>主题</th><th>发件人</th><th>时间</th><th>附件</th></tr></thead>
    <tbody>${(r.mails||[]).map(m=>`<tr>
      <td>${esc(m.subject)}</td><td class="small">${esc(m.from)}</td>
      <td class="small">${esc((m.date||'').slice(0,31))}</td>
      <td class="small">${(m.attachments||[]).map(a=>esc(a)).join('、')||'—'}
        ${m.has_resume?'<span class="chip" style="color:var(--ok);background:var(--ok-soft)">含简历</span>':''}</td>
      </tr>`).join('')||'<tr><td colspan="4">邮箱里没有邮件</td></tr>'}</tbody></table>`;
}

/* ------------------------------ 岗位管理 ------------------------------ */
async function viewOrg(){
  const j = await api('/api/jobs');
  const jobs = j.items || [];
  document.getElementById('view').innerHTML = `
  <div class="card">
    <h2>岗位管理</h2>
    <div class="note" style="margin-top:8px">只有岗位名称必填；JD 留空的项沿用默认尺子。</div>
    <div class="jdform" style="margin-top:12px">
      <label>岗位名称 *</label>
      <input id="jobTitle" placeholder="如：工艺工程师">
      <label>必需技能</label>
      <div class="multi" id="multi_jmust"></div>
      <label>加分技能</label>
      <div class="multi" id="multi_jpref"></div>
      <label>最低学历</label>
      <select id="jobEdu">
        <option value="">（不限 / 沿用默认）</option>
        <option>大专</option><option>本科</option><option>硕士</option><option>博士</option>
      </select>
      <label>最低年限</label>
      <input id="jobYears" list="yearList2" placeholder="不限" style="width:130px">
      <datalist id="yearList2">
        <option value="不限"></option><option value="1"></option><option value="2"></option>
        <option value="3"></option><option value="5"></option><option value="8"></option>
      </datalist>
      <label>专业需求（学科名）</label>
      <div class="multi" id="multi_jmajor"></div>
      <datalist id="majorList"></datalist>
      <label>职责说明</label>
      <textarea id="jobNote" rows="3" placeholder="岗位职责、必须说明的事项（自由文本）"></textarea>
    </div>
    <div class="note" style="margin-top:6px">专业需求填<b>学科名</b>，不要填进「必需技能」。</div>
    <div class="bar" style="margin-top:10px">
      <button class="btn-primary" onclick="addJob()">新增岗位</button>
      <span class="small">技能按逗号 / 顿号 / 分号分隔都认。</span>
    </div>
    <div class="spacer"></div>
    <table><thead><tr><th>岗位</th><th>JD（判断尺子）</th><th class="nw">投递数</th><th class="nw">状态</th><th class="nw">操作</th></tr></thead>
      <tbody>${jobs.map(x=>`<tr>
        <td>${esc(x.title)}</td>
        <td class="small jdsum">${jdSummary(x.jd_json)}</td>
        <td class="nw">${x.applications_count||0}</td>
        <td class="nw">${x.active?'<span class="chip" style="color:var(--ok);background:var(--ok-soft)">开放</span>':'<span class="chip" style="color:var(--ink-3);background:var(--surface-2)">已停用</span>'}</td>
        <td class="nw"><button onclick="editJd(${x.id})">查看 / 编辑 JD</button>
          <button onclick="regradeJob(${x.id})"
            title="按当前 JD 重算该岗位已有投递的建议档位">重新分析</button>
          ${x.active?`<button class="btn-danger" onclick="toggleJob(${x.id},false)">停用</button>`:`<button onclick="toggleJob(${x.id},true)">启用</button>`}</td>
      </tr>`).join('')||'<tr><td colspan="5">暂无岗位</td></tr>'}</tbody></table>
  </div>`;
  // 学科目录补进 <datalist>：HR 填专业需求时能直接选学科名，不用凭记忆写。
  // 这一步只影响输入体验——填错名字不会报错，只会在报告里标「未识别」。
  loadMajorList();
  loadSkillList();          // 技能候选（本体规范名），同样只是输入提示
  // 岗位上那三个多值输入要在 DOM 到位后初始化。
  // 顺序反了就是"控件是空的"——本项目踩过（字典面板那次）。
  JD_MULTI = {jmust: [], jpref: [], jmajor: []};
  multiRender('jmust', 'skillList', '如：真空熔铸，回车添加');
  multiRender('jpref', 'skillList', '如：XRD，回车添加');
  multiRender('jmajor', 'majorList', '如：材料科学与工程，回车添加');
}
// 拉学科目录填进 datalist（失败静默：它只是输入提示，不该挡住岗位管理页面）
async function loadMajorList(){
  const el = document.getElementById('majorList');
  if(!el) return;
  if(el.dataset.loaded==='1') return;
  const d = await api('/api/majors?limit=400');
  if(d.__http_error || !d.items) return;
  el.innerHTML = d.items.map(m=>`<option value="${esc(m.name)}">${esc(m.category)}</option>`).join('');
  el.dataset.loaded = '1';
}
// 技能候选（v1.23.4）：岗位表单的技能输入一直写着 list="skillList"，
// 但这个 datalist **从来不存在** —— 输入框有下拉箭头却点不出东西。
// 用本体里的规范名补上（别名也会出现在候选里，选中后按规范名存）。
async function loadSkillList(){
  const el = document.getElementById('skillList');
  if(!el || el.dataset.loaded==='1') return;
  const d = await api('/api/ontology');
  if(d.__http_error || !d.skills) return;
  el.innerHTML = d.skills.map(s=>`<option value="${esc(s.canonical)}"></option>`).join('');
  el.dataset.loaded = '1';
}
// JD 摘要：列表里一眼看出这个岗位的判断尺子是什么
function jdSummary(jd){
  const must = (jd||{}).must || {}, pref = (jd||{}).preferred || {};
  const parts = [];
  if ((must.skills_required||[]).length) parts.push('必需：' + must.skills_required.join('、'));
  if ((pref.skills||[]).length) parts.push('加分：' + pref.skills.join('、'));
  if (must.education_min) parts.push(must.education_min + '起');
  if (must.years_min) parts.push(must.years_min + ' 年以上');
  if ((must.major_required||[]).length) parts.push('专业：' + must.major_required.join('、'));
  if ((jd||{}).note) parts.push('有职责说明');
  return parts.length ? esc(parts.join(' ｜ '))
    : '<span style="color:var(--ink-3)">沿用默认尺子</span>';
}
// 技能输入切分：逗号 / 顿号 / 分号都认（与后端 _split_skills 同一口径）
function splitSkills(raw){
  return String(raw||'').replace(/[，、；;]/g, ',').split(',')
    .map(s=>s.trim()).filter(Boolean);
}
/* ---- 多值输入组件（v1.23）----
   专业需求/必需技能/加分技能都是"若干个值"，之前是逗号分隔的文本框：
   选一个（datalist）就顶掉前一个，等于多值场景用了单值控件。
   现在：已选显示成标签（点 × 删），输入框里选中或回车就追加一个。
   隐藏域沿用原来的 id（ejMust/ejPref/ejMajor），保存逻辑一行不用改。 */
let JD_MULTI = {};
/* 多值组件的宿主表。key → 隐藏域 id。
   岗位管理页的表单用 jmust/jpref/jmajor 三个 key，与 JD 编辑器的
   must/pref/major 分开——两处同屏出现时不会互相覆盖。 */
const MULTI_IDS = {must:'ejMust', pref:'ejPref', major:'ejMajor',
                   jmust:'jobMust', jpref:'jobPref', jmajor:'jobMajor'};
/* 每个 key 的渲染参数（下拉 id / 占位文字）。
   记下来是为了**重渲染时不丢参数**：原来 multiDel 只能从 DOM 里反查，
   结果 placeholder 会变成空字符串、datalist 也掉了。 */
const MULTI_OPTS = {};
function multiRender(key, listId, ph){
  const box = document.getElementById('multi_'+key);
  if (!box) return;
  if (listId !== undefined && listId !== null) MULTI_OPTS[key] = {listId, ph};
  const o = MULTI_OPTS[key] || {listId: '', ph: ''};
  const vals = JD_MULTI[key] || [];
  box.innerHTML = `${vals.map((v,i)=>`<span class="multi-chip">${esc(v)}
      <b onclick="multiDel('${key}',${i})" title="移除">×</b></span>`).join('')}
    <input id="madd_${key}" list="${o.listId}" placeholder="${esc(o.ph||'输入后回车，或从下拉里选')}"
      onkeydown="if(event.key==='Enter'){event.preventDefault();multiAdd('${key}');}"
      onchange="multiAdd('${key}')">
    <input type="hidden" id="${MULTI_IDS[key]}" value="${esc(vals.join(', '))}">`;
}
function multiAdd(key){
  const inp = document.getElementById('madd_'+key);
  const v = (inp && inp.value || '').trim().replace(/[，、；;]/g, '');
  if (!v){ if (inp) inp.focus(); return; }
  JD_MULTI[key] = JD_MULTI[key] || [];
  if (JD_MULTI[key].indexOf(v) >= 0){ toast('「'+v+'」已经加过了','warn'); inp.value=''; return; }
  JD_MULTI[key].push(v);
  multiRender(key);                       // 参数已记住，重渲染不会丢
  const again = document.getElementById('madd_'+key);
  if (again) again.focus();
}
function multiDel(key, idx){
  (JD_MULTI[key] || []).splice(idx, 1);
  multiRender(key);
}

function jdEditorHtml(jid, jd){
  const must = jd.must || {}, pref = jd.preferred || {};
  const eduOpts = ['','大专','本科','硕士','博士'].map(v =>
    `<option value="${v}" ${v===(must.education_min||'')?'selected':''}>${v||'（不限 / 沿用默认）'}</option>`).join('');
  return `<div class="jdform" style="margin-top:12px">
    <label>必需技能</label>
    <div class="multi" id="multi_must"></div>
    <label>加分技能</label>
    <div class="multi" id="multi_pref"></div>
    <label>最低学历</label><select id="ejEdu">${eduOpts}</select>
    <label>最低年限</label>
    <input id="ejYears" list="yearList" style="width:130px"
           value="${must.years_min==null?'不限':must.years_min}"
           placeholder="不限">
    <datalist id="yearList">
      <option value="不限"></option><option value="1"></option><option value="2"></option>
      <option value="3"></option><option value="5"></option><option value="8"></option>
      <option value="10"></option>
    </datalist>
    <label>专业需求（学科名）</label>
    <div class="multi" id="multi_major"></div>
    <label>职责说明</label>
    <textarea id="ejNote" rows="3">${esc(jd.note||'')}</textarea>
  </div>
  <div class="note" style="margin-top:6px" id="jdHint">专业需求填<b>学科名</b>（认别名，如「材料学」≡「材料科学与工程」），不要填进技能栏。</div>
  <div class="bar" style="margin-top:12px">
    <button class="btn-primary" onclick="saveJd(${jid})">保存 JD</button>
    <button onclick="regradeJob(${jid})">改完重新分析</button>
    <button onclick="closeModal()">取消</button>
    <span class="small">清空某一项 = 该项不再作为门槛。</span>
  </div>
  <div id="ejOut"></div>`;
}
async function editJd(jid){
  const d = await api('/api/jobs/'+jid);
  if (d.__http_error){ toast(d.detail||'读取失败','danger'); return; }
  setModalTitle((d.job.title||'岗位') + ' · 岗位 JD');
  document.getElementById('mBody').innerHTML = jdEditorHtml(jid, d.jd||{});
  document.getElementById('modal').classList.add('on');
  // 多值组件要在 DOM 到位后初始化（顺序反了就是"控件是空的"——踩过）
  JD_MULTI = {
    must: ((d.jd||{}).must||{}).skills_required || [],
    pref: ((d.jd||{}).preferred||{}).skills || [],
    major: ((d.jd||{}).must||{}).major_required || [],
  };
  multiRender('must', 'skillList', '如：真空熔铸，回车添加');
  multiRender('pref', 'skillList', '如：XRD，回车添加');
  multiRender('major', 'majorList', '如：材料科学与工程，回车添加');
}
async function saveJd(jid){
  const yv = (document.getElementById('ejYears').value || '').trim();
  // 「不限」= 0（后端口径：0 表示不设年限门槛），校招岗位就该是这一项。
  // 清空 = 用空值覆盖，而不是"没填就不改"：HR 主动删掉门槛要真的生效
  const yvNum = (yv === '' || yv === '不限') ? 0 : parseInt(yv, 10);
  if (yv !== '' && yv !== '不限' && (isNaN(yvNum) || yvNum < 0)){
    toast('最低年限请填非负数字，或选「不限」','warn'); return;
  }
  const body = {
    must_skills: splitSkills(document.getElementById('ejMust').value),
    preferred_skills: splitSkills(document.getElementById('ejPref').value),
    education_min: document.getElementById('ejEdu').value,
    years_min: yvNum,
    major_required: splitSkills(document.getElementById('ejMajor').value),
    note: document.getElementById('ejNote').value
  };
  const r = await api('/api/jobs/'+jid+'/jd', {method:'POST', body:JSON.stringify(body)});
  if (r.__http_error || r.error){
    document.getElementById('ejOut').innerHTML = '<div class="danger">'+esc(r.detail||r.error||'保存失败')+'</div>';
    return;
  }
  // 「填错格子」提醒：学科名被粘进技能栏，是实测最常见的录入错误，
  // 后果是"全员命中 0 项"，但界面上完全看不出来。保存时就点破，别等 HR 自己发现。
  const warns = (r.warnings||[]).length
    ? `<div class="warn" style="margin-top:10px"><b>录入提醒</b><br>` +
      (r.warnings||[]).map(w=>'· '+esc(w)).join('<br>') + `</div>`
    : '';
  // 有投递的岗位：保存完直接把"要不要重算已有投递"摆在眼前，
  // 否则 HR 改完 JD 看到匹配结果没变，会以为改动没生效。
  if (r.can_regrade){
    document.getElementById('ejOut').innerHTML = warns +
      `<div class="warn" style="margin-top:10px">JD 已保存。该岗位已有 <b>${r.applications_count}</b> 条投递，
       它们的建议档位仍是旧尺子算的。
       <div class="bar" style="margin-top:8px">
         <button class="btn-primary" onclick="regradeJob(${jid})">按新尺子重新分析这 ${r.applications_count} 条</button>
         <button onclick="closeModal();refresh()">先不动</button>
       </div></div>`;
    toast('JD 已保存；可立即对该岗位已有投递重新分析','ok');
  } else if (warns) {
    document.getElementById('ejOut').innerHTML = warns;
    toast('JD 已保存，但有录入提醒，请看一眼','warn');
  } else {
    toast(r.note||'JD 已更新','ok'); closeModal(); refresh();
  }
}

/* -------- 重新分析（JD 改完后按新尺子重算已有投递；默认先看不改） -------- */
async function regradeJob(jid, apply){
  setModalTitle('重新分析 · 按当前 JD 重算已有投递');
  document.getElementById('mBody').innerHTML =
    `<div class="note">正在按当前 JD 重算该岗位下的投递…（从简历原文重跑规则通道，不调模型，结果可重复）</div>`;
  document.getElementById('modal').classList.add('on');
  const r = await api('/api/jobs/'+jid+'/regrade',
    {method:'POST', body:JSON.stringify({apply: !!apply})});
  if (r.__http_error || r.error){
    document.getElementById('mBody').innerHTML =
      '<div class="danger">'+esc(r.detail||r.error||'重算失败')+'</div>';
    return;
  }
  const rows = (r.items||[]).map(x=>{
    const tcol = k => (TIER_COLOR[k]||TIER_COLOR.D)[0];
    let diff;
    if (x.skipped){
      diff = `<span class="small warn-txt">${esc(x.skipped)}</span>`;
    } else if (x.kept && (x.old_tier!==x.new_tier)){
      // 已确认档位变了差异：如实呈现，但明确标出"未修改"
      diff = `<span class="small">建议 ${esc(x.old_tier||'—')} → ${esc(x.new_tier||'—')}
        <span class="chip" style="color:var(--ink-3);background:var(--surface-2)">HR 已确认，未改动</span></span>`;
    } else if (x.changed){
      diff = `<b style="color:${tcol(x.old_tier)}">${esc(x.old_tier||'—')}</b>
        → <b style="color:${tcol(x.new_tier)}">${esc(x.new_tier||'—')}</b>
        <span class="small">（建议档位${r.applied?'已更新':'将更新'}）</span>`;
    } else {
      diff = '<span class="small" style="color:var(--ink-3)">无变化</span>';
    }
    const why = x.changed && (x.reasons||[]).length
      ? `<div class="small">${esc((x.reasons||[]).slice(0,2).join('；'))}</div>` : '';
    const note = x.note ? `<div class="small warn-txt">${esc(x.note)}</div>` : '';
    return `<tr>
      <td><b>${esc(x.name||'未识别')}</b><div class="small">投递 #${x.application_id}</div></td>
      <td class="nw small">${esc(x.tier_source||'')}</td>
      <td class="nw">${diff}</td>
      <td>${why}${note}</td></tr>`;
  }).join('');
  document.getElementById('mBody').innerHTML = `
    <div class="srcbox">岗位 <b>${esc((r.job||{}).title||'')}</b>
      ｜尺子来源 ${esc(r.jd_source||'')}
      ｜共 ${r.total||0} 条：建议档位${r.applied?'变化':'将变化'} <b>${r.changed||0}</b> 条、
      已确认未改动 ${r.kept_hr_confirmed||0} 条、无法重算 ${r.cannot_regrade||0} 条</div>
    <div class="${r.changed? 'warn':'ok'}" style="margin-top:10px">${esc(r.note||'')}</div>
    <table style="margin-top:12px"><thead><tr><th>候选人</th><th class="nw">档位来源</th>
      <th class="nw">建议档位</th><th>原因 / 说明</th></tr></thead>
      <tbody>${rows||'<tr><td colspan="4">该岗位下没有投递记录</td></tr>'}</tbody></table>
    <div class="note" style="margin-top:10px">${esc(r.channel_note||'')}</div>
    <div class="bar" style="margin-top:12px">
      ${(!r.applied && r.changed) ? `<button class="btn-primary" onclick="regradeJob(${jid},true)">应用重算结果（改 ${r.changed} 条）</button>` : ''}
      <button onclick="closeModal();refresh()">关闭</button>
      <span class="small">重算只改「系统建议」，不会动阶段、不会动备注、不动 HR 已确认的档位。</span>
    </div>`;
  // 落库后立刻刷一次背景（统计卡 + 当前列表）。否则弹层关掉之前，顶部的档位分布
  // 还是应用前的旧数字——「点了应用，可数字没变」是最容易被怀疑"根本没生效"的地方。
  // refresh() 只重画 #stats 与当前视图，不碰弹层，所以不会把结果表冲掉。
  if (r.applied) refresh();
}
async function addJob(){
  const title = document.getElementById('jobTitle').value.trim();
  if(!title){toast('请输入岗位名称','warn');return;}
  const must = splitSkills(document.getElementById('jobMust').value);
  const pref = splitSkills(document.getElementById('jobPref').value);
  const edu  = document.getElementById('jobEdu').value;
  const yv   = document.getElementById('jobYears').value;
  const mreq = splitSkills(document.getElementById('jobMajor').value);
  const note = document.getElementById('jobNote').value.trim();
  // 只写岗位名 + JD 即可：不填部门（不做部门归属），字段留空不影响任何判定
  const body = {title:title};
  // 只提交真正填了的 JD 项：没填的沿用默认尺子，而不是被空值覆盖
  if(must.length) body.must_skills = must;
  if(pref.length) body.preferred_skills = pref;
  if(edu) body.education_min = edu;
  // v1.23：「不限」= 0（后端 0 即不设年限门槛）；校招岗位常用这一项。
  const yvNum = (yv==='' || yv==='不限') ? 0 : parseInt(yv, 10);
  if (yv!=='' && yv!=='不限' && (isNaN(yvNum) || yvNum < 0)){
    toast('最低年限请填非负数字，或选「不限」','warn'); return;
  }
  if(yv!=='') body.years_min = yvNum;
  if(mreq.length) body.major_required = mreq;
  if(note) body.note = note;
  const r = await api('/api/jobs',{method:'POST',body:JSON.stringify(body)});
  if(r.__http_error||r.error){toast(r.detail||r.error||'新增失败','danger');return;}
  const jdFilled = must.length||pref.length||edu||yv!==''||mreq.length||note;
  toast(jdFilled?'岗位已创建，JD 已写入判断尺子':'岗位已创建（JD 沿用默认尺子）','ok');
  if((r.warnings||[]).length){
    // 学科名被填进技能栏是最常见的录入错误，且后果（全员命中 0 项）在列表上完全看不出来，
    // 所以不吞掉：直接弹出来让 HR 当场改。
    alert('录入提醒：\\n\\n' + r.warnings.join('\\n\\n'));
  }
  refresh();
}
async function toggleJob(id, active){
  const r = await api('/api/jobs/'+id+(active?'/activate':'/deactivate'),{method:'POST'});
  if(r.__http_error||r.error){toast(r.detail||r.error||'操作失败','danger');return;}
  toast(active?'岗位已启用':'岗位已停用（未删除）',active?'ok':'info'); refresh();
}

/* ------------------------------ 提案与审计 ------------------------------ */
async function renderPropsInto(boxId){
  const box = document.getElementById(boxId);
  if (!box) return;
  const p = await api('/api/proposals');
  const all = p.items || [];
  const auto = all.filter(x=>x.source==='agent_auto' && x.status==='待确认');
  const srcTag = x => x.source === 'agent_auto'
    ? '<span style="color:var(--accent-ink);font-weight:500">系统巡检</span>'
    : '<span class="small">对话产生</span>';
  box.innerHTML = `
  <div class="panel"><h2>待确认提案</h2>
    <div class="note">在你确认之前，人才库没有任何改动。</div>
    ${auto.length?`<div class="bar" style="margin-top:8px">
      <span class="small">其中 <b>${auto.length}</b> 条来自系统巡检</span>
      <button class="btn-ok" onclick="decideMany('approve')">全部确认执行</button>
      <button onclick="decideMany('reject')">全部拒绝</button></div>`:''}
    <div class="spacer"></div>
    <table><thead><tr><th>#</th><th>来源</th><th>类型</th><th>内容</th><th>风险</th><th>状态</th>
      <th>提交时间</th><th>操作</th></tr></thead>
      <tbody>${all.map(x=>`<tr>
        <td>${x.id}</td><td>${srcTag(x)}</td>
        <td>${esc(x.tool_label || x.tool)}</td><td>${esc(x.summary)}</td>
        <td>${esc(x.risk)}</td><td>${esc(x.status)}</td><td class="small">${esc(x.created_at||'')}</td>
        <td>${x.status==='待确认'?`<button class="btn-ok"
            onclick="decide(${x.id},'approve')">确认执行</button>
          <button class="btn-danger"
            onclick="decide(${x.id},'reject')">拒绝</button>`:'—'}</td>
        </tr>`).join('')||'<tr><td colspan="8">暂无提案</td></tr>'}</tbody></table>
  </div>
  `;
  /* v1.25：这里原来的「操作审计（最近 40 条）」整块已删除（用户要求）。
     理由：那一份是**系统所有操作**的全量日志（含"谁看了谁"的浏览记录），
     对 HR 是噪音——"只有改动才值得占版面"，而改动记录在
     **每个候选人自己的档案页**里，那才是 HR 真正会去的地方。
     ⚠️ 只是**不再展示**，数据一条没删：`audit_log` 照旧全量落库，
     查法是 `/api/audit?include_readonly=1`，智能助手的 list_audit 也还能用。
     所以"以后想查还能查到"，不是"把记录毁了"。 */
}

/* 批量处理「系统巡检」提案：逐条走同一个 decide 接口，
   一条失败不影响其余——不搞"整批原子提交"，因为每条本来就是独立决定。 */
async function decideMany(decision){
  const p = await api('/api/proposals');
  const ids = (p.items||[]).filter(x=>x.source==='agent_auto' && x.status==='待确认')
                           .map(x=>x.id);
  if (!ids.length){ toast('没有待处理的系统提案','warn'); return; }
  if (!await askConfirm(`将${decision==='approve'?'确认执行':'拒绝'} ${ids.length} 条系统提案，继续？`)) return;
  let ok = 0, fail = 0;
  for (const id of ids){
    const r = await api('/api/proposals/'+id+'/decide',
                        {method:'POST', body:JSON.stringify({decision:decision})});
    if (r.__http_error || r.error) fail++; else ok++;
  }
  toast(`${ok} 条已处理${fail?('，'+fail+' 条失败'):''}`, fail?'warn':'ok');
  await boot();
}

async function decide(pid, decision){
  const r = await api('/api/proposals/'+pid+'/decide', {method:'POST', body:JSON.stringify({decision:decision})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'处理失败','danger'); return; }
  toast(decision==='approve' ? ('提案 #'+pid+' 已执行并生效') : ('提案 #'+pid+' 已拒绝'), 'ok');
  await boot();
}

/* ------------------------------ 检索 ------------------------------ */
/* 检索（原独立页，已并入人才库，折叠展示）：
   技能召回走本体精确匹配、语义召回用于说不清关键词的探索——两者都在这里。 */
async function renderSearchInto(boxId){
  const box = document.getElementById(boxId);
  if (!box) return;
  const s = META.search || {};
  box.innerHTML = `
  <div class="panel">
    <div class="flexbetween">
      <div><h2 style="margin:0">人才检索</h2>
        <div class="note">索引模型 <b>${esc(s.index_model||'未建立')}</b>，已索引 ${s.indexed||0}/${s.people||0} 人。</div></div>
      <div class="bar"><button onclick="go('sys')">导入与来源配置在系统配置 →</button>
        <button onclick="refresh()">刷新索引状态</button>
        <span class="badge plain" style="background:var(--surface-2);color:var(--ink-3)">${esc(s.model||'')}</span></div>
    </div>
  </div>
  <div class="card">
    <h2>① 技能召回（推荐）</h2>
    <div class="bar">
      <input id="skInput" placeholder="技能，逗号分隔，如：真空熔铸,钛合金" style="width:340px"
             onkeydown="if(event.key==='Enter'){doSkillSearch()}">
      <select id="skMode"><option value="all">需全部具备</option><option value="any">具备其一即可</option></select>
      <button class="btn-primary" onclick="doSkillSearch()">检索</button>
    </div>
    <div id="skOut" style="margin-top:12px"></div>
  </div>
  <div class="card">
    <h2>② 语义召回</h2>
    <div class="bar">
      <input id="semInput" placeholder="自然语言，如：有难熔合金研发背景的博士" style="width:340px"
             onkeydown="if(event.key==='Enter'){doSemSearch()}">
      <button class="btn-primary" onclick="doSemSearch()">检索</button>
    </div>
    <div id="semOut" style="margin-top:12px"></div>
  </div>`;
}
async function doSkillSearch(){
  const raw = document.getElementById('skInput').value || '';
  const mode = document.getElementById('skMode').value;
  const out = document.getElementById('skOut');
  if (!raw.trim()){ out.innerHTML = '<div class="warn">请输入技能关键词</div>'; return; }
  out.innerHTML = '<div class="note">检索中…</div>';
  const r = await api('/api/search/skills?skills='+encodeURIComponent(raw)+'&mode='+mode);
  if (r.__http_error){ out.innerHTML = '<div class="danger">'+esc(r.detail||'失败')+'</div>'; return; }
  out.innerHTML = `<div class="note">归一后：${esc((r.resolved||[]).join('、'))}
    ${(r.unknown_terms||[]).length?('｜ 本体外词：'+esc(r.unknown_terms.join('、'))):''}
    ｜ 命中 <b>${r.count}</b> 人</div>
    ${(r.results||[]).map(x=>`<div class="card" style="margin-top:8px">
      <div class="nm">${esc(x.name||'未识别')} <span class="small">#${x.candidate_id}</span></div>
      <div class="meta">${esc(x.education||'—')} · ${x.years==null?'—':x.years+' 年'} · 档 ${esc(x.tier||'—')}</div>
      <div class="meta" style="margin-top:2px"><b>联系方式</b>：${contactLine(x)}</div>
      <div style="margin-top:6px">${(x.skills||[]).map(k=>`<span class="chip" style="color:var(--accent);background:var(--accent-soft)">${esc(k)}</span>`).join('')}</div>
      ${Object.entries(x.evidence||{}).map(([k,v])=>`<div class="ev">证据[${esc(k)}]：${esc(v)}</div>`).join('')}
      <div class="acts"><button onclick="showDetail(${x.candidate_id})">完整档案</button></div>
      </div>`).join('')||'<div class="card" style="margin-top:8px">没有同时具备这些技能的人。可切换为「具备其一」再试。</div>'}`;
}
async function doSearch(){
  KW = document.getElementById('kwBox').value || '';
  poolPageReset(); refresh();
}
async function doSemSearch(){
  const q = document.getElementById('semInput').value || '';
  const out = document.getElementById('semOut');
  if (!q.trim()){ out.innerHTML = '<div class="warn">请输入检索语句</div>'; return; }
  out.innerHTML = '<div class="note">检索中…</div>';
  const r = await api('/api/search/semantic?q='+encodeURIComponent(q)+'&top_k=8');
  if (r.__http_error){ out.innerHTML = '<div class="danger">'+esc(r.detail||'失败')+'</div>'; return; }
  // v1.14：降级提示只在**真正用到语义检索时**才说，且要说清后果与出路。
  // 原来每次都把「embeddinggemma:latest · 服务不可达（降级中）」挂在结果顶部，
  // 对十几人的库这是噪音，看起来像系统故障（HR 反馈）。
  _semDegraded = !!r.error;
  out.innerHTML = '<div class="note">索引：' + esc(r.model || '—')
    + (r.error
      ? ('｜<b>已降级为本地哈希向量</b>：只按字面重叠匹配，简历措辞不同就找不到。'
         + '<span class="small">想要真正的语义匹配，可在电脑上跑一个本地向量模型'
         + '（如 Ollama + bge-m3，仍不出内网、不外传简历）。</span>')
      : (r.note ? ('｜' + esc(r.note)) : '')) + '</div>' + `
    <table><thead><tr><th>相似度</th><th>姓名</th><th>学历/年限</th><th>档位</th><th>联系方式</th><th>技能</th><th></th></tr></thead>
    <tbody>${(r.results||[]).map(x=>`<tr>
      <td>${x.score}</td><td>${esc(x.name||'未识别')}</td>
      <td>${esc(x.education||'—')} · ${x.years==null?'—':x.years+' 年'}</td>
      <td>${esc(x.tier||'—')}</td><td>${contactLine(x)}</td>
      <td class="small">${esc((x.skills||[]).join('、'))}</td>
      <td><button onclick="showDetail(${x.candidate_id})">档案</button></td></tr>`).join('')
      ||'<tr><td colspan="7">无结果</td></tr>'}</tbody></table>
    ;`;
}

/* ------------------------------ 写邮件 ------------------------------ */
/* 口径：生成全自动、发送必须人工确认。页面上刻意不提供"自动发送"开关。
   模板是 HR 写的固定措辞，系统只做 {变量} 替换；取不到值就标成【待填：xxx】。 */
let MAIL_TPL = [];
let IDICT = {};          // 面试字典（单位/会议室/时段/联系人）
// 变量清单（内置 + 运行时）：模板编辑时渲染成可点的按钮，HR 不用记变量名
let MAIL_VARS = {builtin: [], runtime: []};

function mailVarChanged(key){
  // ① 日期 → 自动算出星期几，HR 不用手填"（周三）"
  if (key === '面试时间'){
    const v = document.getElementById('mv_面试时间');
    if (!v || !v.value) return;
    const d = new Date(v.value + 'T00:00:00');
    const wk = ['周日','周一','周二','周三','周四','周五','周六'][d.getDay()];
    v.dataset.weekday = wk;
  }
  // ② 方式 → 线上面试才要会议号；现场面试把会议号清掉并隐藏
  if (key === '面试方式'){
    const mode = (document.getElementById('mv_面试方式')||{}).value || '';
    // 只认"含线上"两个字；其它写法（如"腾讯会议"）按现场处理，不擅自推断
    const online = mode.indexOf('线上') >= 0;
    const box = document.getElementById('mv_会议号');
    const hint = document.getElementById('hintMeet');
    if (box) box.style.display = online ? '' : 'none';
    if (hint) hint.style.display = online ? '' : 'none';
    if (!online && box) box.value = '';
  }
  // ③ 联系人 → 自动带出电话（字典联动，不用手抄）。
  //    现在是组合框（可手打），所以按"输入的名字"去字典里找电话；
  //    手打了字典外的名字就找不到电话——这是如实反馈，不编号码。
  if (key === '联系人'){
    const inp = document.getElementById('mv_联系人');
    const tel = document.getElementById('mv_联系电话');
    if (!inp || !tel) return;
    const who = (inp.value || '').trim();
    const hit = (IDICT.contacts||[]).find(
      c => c.name === who || (c.dept + ' · ' + c.name) === who);
    tel.value = hit ? (hit.phone || '') : '';
    tel.placeholder = hit ? '' : '字典里没这个人，请手动填写';
  }
}

async function viewMail(){
  const [tpl, smtp, cands, dict] = await Promise.all([
    api('/api/mail/templates'), api('/api/mail/smtp'), api('/api/candidates'),
    api('/api/interview-dict').catch(()=>({}))]);
  IDICT = dict || {};
  MAIL_TPL = tpl.items || [];
  MAIL_VARS = {builtin: tpl.builtin_vars || [], runtime: tpl.runtime_vars || []};
  const conf = smtp || {};
  const cands_ = cands.items || [];
  // 存全量候选人（按姓氏筛选收件人用）
  window.MAIL_CANDS = cands_.map(c => ({
    id: c.id, name: c.name || ('未识别#'+c.id), email: c.email || '' }));
  const tlist = MAIL_TPL.map(t =>
    `<option value="${t.id}">${esc(t.name)}${t.scene?('（'+esc(t.scene)+'）'):''}</option>`).join('');
  // v1.20：这些字段不该是"带灰色提示的文本框"——那是让 HR 凭记忆敲键盘。
  // 按 key 分派成真控件：日期/时段/地点/单位/方式/会议号/联系人联动。
  // v1.21：**组合框**——点右边能下拉选，也能直接打字。
  // 原生 <input list> 实现，零 JS 依赖；值照样进模板变量。
  const _dlId = k => 'dl_' + String(k).replace(/[^\\w一-龥]/g, '');
  const _combo = (key, list, ph) => `
    <input id="mv_${esc(key)}" list="${_dlId(key)}" style="width:70%"
      placeholder="${esc(ph||'可直接选择，或自己输入')}"
      oninput="mailVarChanged('${esc(key)}')" onchange="mailVarChanged('${esc(key)}')">
    <datalist id="${_dlId(key)}">
      ${(list||[]).map(x=>`<option value="${esc(x)}"></option>`).join('')}
    </datalist>`;
  const _contactOpts = (IDICT.contacts||[]).map(
    c => `<option value="${esc(c.name)}" data-phone="${esc(c.phone||'')}"
            data-dept="${esc(c.dept||'')}">${esc(c.dept?c.dept+' · ':'')}${esc(c.name)}</option>`).join('');
  const FIELD_UI = {
    '面试时间': () => `<input id="mv_面试时间" type="date" style="width:70%"
        onchange="mailVarChanged('面试时间')">`,
    '面试时段': () => _combo('面试时段', IDICT.slots, '如 09:00-10:00'),
    '面试地点': () => _combo('面试地点', IDICT.rooms, '选1519，或直接输入别的会议室'),
    '面试单位': () => _combo('面试单位', IDICT.units, '选单位，或直接输入'),
    '面试方式': () => _combo('面试方式', IDICT.modes, '现场面试 / 线上面试'),
    '会议号': () => `<input id="mv_会议号" style="width:70%" placeholder="线上面试的会议号或入会链接">
        <div class="small" id="hintMeet" style="color:var(--bad);display:none">
          选了线上面试就要填会议号，否则候选人收不到入会方式</div>`,
    '联系人': () => `
      <input id="mv_联系人" list="dl_联系人" style="width:70%"
        placeholder="点选或直接输入姓名"
        oninput="mailVarChanged('联系人')" onchange="mailVarChanged('联系人')">
      <datalist id="dl_联系人">${_contactOpts}</datalist>
      <div class="small">选好后自动带出电话；也可以直接手打。字典在「系统配置 → 面试字典」维护。</div>`,
    '联系电话': () => `<input id="mv_联系电话" style="width:70%" readonly
        placeholder="选联系人后自动带出">`,
  };
  const rvars = (tpl.runtime_vars || []).map(v => {
    const f = FIELD_UI[v.key];
    return `<div class="k">${esc(v.key)}</div><div>${f ? f() :
      `<input id="mv_${esc(v.key)}" style="width:70%" placeholder="${esc(v.desc)}">`}</div>`;
  }).join('');
  const varChips = [...(tpl.builtin_vars||[]), ...(tpl.runtime_vars||[])]
    .map(v => `<code title="${esc(v.desc)}">{${esc(v.key)}}</code>`).join(' ');

  document.getElementById('view').innerHTML = `
  <div class="panel">
    <h2>写邮件</h2>
    <div class="note">发送必须由你点确认。变量取不到值会标成 <code>【待填：xxx】</code>。</div>
    <div class="note">发信账号：${conf.user
        ? `<b>${esc(conf.user)}</b>（${esc(conf.host)}:${conf.port}，${conf.ssl?'SSL':'STARTTLS'}）`
        : '<span style="color:var(--bad)">未配置</span>'}
      <button onclick="checkSmtp()" style="margin-left:6px">检查发信配置</button>
      <span id="smtpMsg" class="small"></span></div>
    <div class="small" style="margin-top:8px">收发信配置（邮箱账号、授权码、来源目录、模板管理）都在
      <a href="javascript:go('sys')" style="color:var(--accent-ink)">系统配置</a> 里。</div>
  </div>
  <div class="card"><h2>第一步 · 选人、选模板</h2>
    <div class="kv">
      <div class="k">收件人</div><div style="position:relative">
        <input id="mCandInput" style="width:55%" placeholder="输入姓名或邮箱前缀，点选或回车确认"
               autocomplete="off"
               oninput="filterCand(true)" onfocus="filterCand()" onkeydown="candKey(event)">
        <input id="mCand" type="hidden" value="">
        <!-- 给代码读的收件人邮箱（显示给人看的是下面那块醒目样式，见 setMailTo） -->
        <input id="mToVal" type="hidden" value="">
        <div class="cand-drop" id="mCandDrop" style="display:none"></div>
        <!-- 收件人邮箱（v1.24.9）：选人后**显眼**展示。
             原来是小灰字跟在输入框后面（"放在后面不够显眼"），而发信时 HR 要靠它
             核对收件人——邮箱错一位就是发错人，必须一眼看清、字号够大。 -->
        <div id="mTo" style="display:none;margin-top:8px"></div>
        <div class="small" style="color:var(--ink-3)">点开就列出候选人；输入姓名/邮箱则按前缀筛选，↑↓ 移动、Enter 确认。</div></div>
      <div class="k">模板</div><div>
        <select id="mTpl" style="width:55%">
          <option value="">（不用模板，直接手写）</option>${tlist}</select>
        ${MAIL_TPL.length ? '' : '<span class="small">还没有模板 —— 去「系统配置 → 模板管理」新建一个</span>'}</div>
      ${rvars}
    </div>
    <div class="bar" style="margin-top:10px">
      <button class="btn-primary" onclick="genMailDraft()">生成草稿</button>
      <span class="small">这一步只生成内容，不会发送</span>
    </div>
  </div>
  <div class="card"><h2>第二步 · 预览与修改</h2>
    <div class="kv">
      <div class="k">主题</div><div><input id="mSubject" style="width:100%"></div>
      <div class="k">正文</div><div>
        ${richEditorHtml('mailEditor')}
        <div class="small" style="margin-top:6px">
          纯文字通知按纯文本发送；带了表格/加粗则自动按 HTML 邮件发送，
          并同时附一份纯文本版本（不显示 HTML 的客户端也能读）。
          正文里<b>不要</b>留【待填：xxx】，没填好会被拦下不让发。
        </div>
      </div>
    </div>
    <div id="mMiss" class="small" style="margin-top:6px"></div>
    <div class="bar" style="margin-top:10px">
      <button class="btn-ok" id="mailSendBtn" onclick="sendMailConfirm()">确认发送</button>
      <button onclick="saveCurrentAsTemplate()">把当前正文存为模板</button>
      <span class="small">点击后会再确认一次收件人与主题；发送动作写入审计</span>
    </div>
    <!-- 发信结果留在这里：成功的凭据（收件人/主题/时间/发件人显示名）要能事后核对，
         失败也要留住原因，别只靠顶部那条 7 秒就消失的提示。 -->
    <div id="mailSentBox"></div>
  </div>
`;
  // v1.25：若是从对话草稿卡"在编辑器中修改"切过来的，自动把草稿填进编辑器。
  // 放在渲染末尾（编辑器元素已存在），且消费后清空，避免后续普通进邮件页也被填上旧草稿。
  if (PENDING_DRAFT){
    applyDraftToMailer(PENDING_DRAFT);
    PENDING_DRAFT = null;
  }
}

function applySmtpPreset(){
  const sel = document.getElementById('spPreset');
  if (!sel || !sel.value) return;
  const [host, port, ssl] = sel.value.split('|');
  document.getElementById('spHost').value = host;
  document.getElementById('spPort').value = port;
  document.getElementById('spSsl').checked = (ssl === '1');
  toast('已填入 ' + host + '（端口 ' + port + '）', 'info');
}

async function saveSmtp(){
  const msg = document.getElementById('smtpMsg');
  msg.textContent = '保存中…';
  const payload = {
    host: document.getElementById('spHost').value.trim(),
    port: parseInt(document.getElementById('spPort').value, 10) || 465,
    ssl: document.getElementById('spSsl').checked,
    user: document.getElementById('spUser').value.trim(),
    from_name: document.getElementById('spFrom').value.trim()
  };
  const pwd = document.getElementById('spPwd').value;
  if (pwd) payload.password = pwd;         // 留空 = 不修改已保存的授权码
  const r = await api('/api/mail/smtp', {method:'POST', body:JSON.stringify(payload)});
  if (r.__http_error || r.error){ msg.textContent = r.detail || r.error || '保存失败'; return; }
  toast('发信配置已保存', 'ok');
  await viewMail();
}

/* 邮件模板管理（原在写邮件页，已并入「系统配置」）。模板由 HR 写，系统只做变量替换。 */
async function renderTplMgrInto(boxId){
  const box = document.getElementById(boxId);
  if (!box) return;
  const tpl = await api('/api/mail/templates');
  MAIL_TPL = tpl.items || [];
  MAIL_VARS = {builtin: tpl.builtin_vars || [], runtime: tpl.runtime_vars || []};
  const varChips = [...(tpl.builtin_vars||[]), ...(tpl.runtime_vars||[])]
    .map(v => `<code title="${esc(v.desc)}">{${esc(v.key)}}</code>`).join(' ');
  box.innerHTML = `
  <div class="card"><h2>模板管理</h2>
    <div class="note">变量点一下插到光标处。
      <br>可用变量：${varChips}</div>
    <div class="spacer"></div>
    <table><thead><tr><th>#</th><th>模板名</th><th>场景</th><th>主题</th><th>操作</th></tr></thead>
      <tbody>${MAIL_TPL.map(t=>`<tr>
        <td>${t.id}</td><td>${esc(t.name)}</td><td>${esc(t.scene||'')}</td>
        <td class="small">${esc((t.subject||'').slice(0,40))}</td>
        <td><button onclick="editTpl(${t.id})">编辑</button>
            <button class="btn-danger" onclick="delTpl(${t.id},'${esc(t.name)}')">删除</button></td>
        </tr>`).join('') || '<tr><td colspan="5">还没有模板</td></tr>'}</tbody></table>
    <div class="bar" style="margin-top:10px">
      <button onclick="newTpl()">新建模板</button>
      <span class="small" id="tplMsg"></span></div>
    <div id="tplForm"></div>
  </div>`;
}

/* 原来这里有个 fillMailTo()，是<mCand> 还是<select> 时代的遗留（从 option 文本里
   split(' · ') 反解邮箱）。v1.24.9 收件人改成可输入组合框后 mCand 变成 hidden input，
   这个函数已经没有任何调用点、也永远不会工作——删掉，免得日后有人以为能用。 */

/* 收件人可输入筛选（v1.24.9）：输入姓名/姓氏，实时列出匹配候选人，点选填入。
   底层 mCand（hidden）仍存 candidate_id，genMailDraft 拿它发草稿，数据流不变。 */
/* 收件人输入 + 前缀匹配补全（v1.24.9）
   要的行为：输入框**自由打字**（不被下拉锁死），同时下方下拉做**前缀匹配**
   （输入到哪匹配到哪），点一下选中并回填。
   为什么不用原生 <datalist>：它按 option 的 value 整体前缀匹配，而这里的 value
   是"姓名 · 邮箱"，输入"林"匹配不到；且各浏览器行为不一致、无法加键盘导航。
   ——所以自己实现一个可控的补全框。 */
let _candHi = -1;   // 当前高亮项下标（键盘上下用）
function filterCand(clearSel){
  const inp = document.getElementById('mCandInput');
  const drop = document.getElementById('mCandDrop');
  if (!inp || !drop) return;
  const q = (inp.value || '').trim();
  // 输入内容一旦变动，就把"已选"清掉——不能让上一份简历的 id 留着发错人。
  // clearSel 只在 oninput 时传 true；onfocus 不传，否则点选后一聚焦就把 id 清了。
  if (clearSel){
    document.getElementById('mCand').value = '';
    setMailTo('');
  }
  // **空输入也弹下拉**（v1.24.9）：点进输入框就该看到候选人列表，
  // 不该要求"先打字才给看"——HR 常常是先浏览再决定输什么。
  // 前缀为空 = 全部命中（startsWith('') 恒真），所以下面不用特判空串。
  const lq = q.toLowerCase();
  const list = (window.MAIL_CANDS || []).filter(c =>
    (c.name || '').toLowerCase().startsWith(lq) ||
    (c.email || '').toLowerCase().startsWith(lq));
  _candHi = list.length ? 0 : -1;
  if (!list.length){
    // 没匹配上时也把框显示出来，写明"没有匹配的人"——
    // 静默不弹会让人以为功能坏了。
    drop.innerHTML = '<div class="cand-item" style="color:var(--ink-3);cursor:default">'
      + '没有匹配「' + esc(q) + '」的候选人</div>';
    drop.style.display = 'block';
    return;
  }
  drop.innerHTML = list.slice(0, 20).map((c, i) =>
    `<div class="cand-item${i === _candHi ? ' hi' : ''}" data-cid="${c.id}">`
    + `<b>${esc(c.name)}</b> <span class="em">${esc(c.email || '无邮箱')}</span></div>`
  ).join('');
  drop.style.display = 'block';
  // 事件委托：点候选项回填。用 mousedown 而非 click——早于输入框失焦，
  // 否则 blur 先跑、下拉已被收起，点不中。
  drop.onmousedown = ev => {
    const el = ev.target.closest('.cand-item');
    if (el) pickCand(el.dataset.cid);
  };
}
function candHighlight(i){
  const drop = document.getElementById('mCandDrop');
  if (!drop) return;
  const items = drop.querySelectorAll('.cand-item');
  if (!items.length) return;
  _candHi = (i + items.length) % items.length;
  items.forEach((el, k) => el.classList.toggle('hi', k === _candHi));
  items[_candHi].scrollIntoView({block: 'nearest'});
}
// 点选：回填姓名、锁定 candidate_id、显示邮箱
function pickCand(cid){
  const c = (window.MAIL_CANDS || []).find(x => x.id === +cid);
  if (!c) return;
  document.getElementById('mCandInput').value = c.name;
  document.getElementById('mCand').value = c.id;
  document.getElementById('mCandDrop').style.display = 'none';
  setMailTo(c.email, c.name);
}
// 键盘：上下选、Enter 选中、Esc 收起
function candKey(e){
  const drop = document.getElementById('mCandDrop');
  if (!drop || drop.style.display === 'none'){
    if (e.key === 'ArrowDown') filterCand();
    return;
  }
  if (e.key === 'ArrowDown'){ e.preventDefault(); candHighlight(_candHi + 1); }
  else if (e.key === 'ArrowUp'){ e.preventDefault(); candHighlight(_candHi - 1); }
  else if (e.key === 'Enter'){
    const el = drop.querySelectorAll('.cand-item')[_candHi];
    if (el){ e.preventDefault(); pickCand(el.dataset.cid); }
  }
  else if (e.key === 'Escape'){ drop.style.display = 'none'; }
}
// 点别处收起补全框（用 mousedown，比 click 早，能抢在输入框失焦前）
document.addEventListener('mousedown', e => {
  const drop = document.getElementById('mCandDrop');
  if (drop && e.target !== document.getElementById('mCandInput')
      && !drop.contains(e.target)) drop.style.display = 'none';
});

function mailRuntime(){
  const out = {};
  document.querySelectorAll('[id^="mv_"]').forEach(el => {
    const k = el.id.slice(3);
    if (el.value.trim()) out[k] = el.value.trim();
  });
  return out;
}

/* ===================== 所见即所得编辑器（邮件正文 + 邮件模板共用） =====================
   为什么不做"管道符语法 + 预览框"：HR 要的是**直接画表格**（163 邮箱那种），
   写 `| 列 | 列 |` 再去看预览，等于让人先学一套语法再验一遍——两步都多余。
   为什么模板编辑也用这个编辑器：模板里存的就是版式，让 HR 看 `<table style="...">`
   这种源码等于不让他改版式。**看得见才能调**，所以模板编辑一律走可视化编辑器。

   实现只用 `contenteditable` + 少量 DOM 操作：本地单角色、离线单页应用，
   引富文本框架（几 MB）不划算。`execCommand` 虽被标为废弃，但主流浏览器仍在实现，
   "加粗/斜体/下划线/insertHTML/insertText"够用；表格结构操作（加行/加列/合并/对齐）
   它管不了，那部分用 DOM 直接做。

   两个细节必须处理，否则按钮会"看起来没反应"：
   1. **光标会丢**：点工具栏按钮时编辑器失焦，选择区可能被清掉 →
       用 selectionchange 记住最后一次落在编辑器内的 range，操作前恢复它；
   2. **多个编辑器共存**（正文、模板）→ 所有函数都带 `id` 参数，对话框用 EDITOR_TARGET
       记住这次要操作哪个。 */
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

let EDITOR_TARGET = 'mailEditor';      // 对话框要操作哪个编辑器
let EDITOR_SEL = null;                 // 最后一次落在编辑器内的选区 {id, range}

document.addEventListener('selectionchange', () => {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return;
  let n = sel.anchorNode;
  if (n && n.nodeType !== 1) n = n.parentNode;
  const host = (n && n.closest) ? n.closest('.richeditor') : null;
  if (host) EDITOR_SEL = {id: host.id, range: sel.getRangeAt(0).cloneRange()};
});

function editorEl(id){ return document.getElementById(id || 'mailEditor'); }
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
  EDITOR_TARGET = id || 'mailEditor';
  setModalTitle('插入表格');
  document.getElementById('mBody').innerHTML = `
    <div class="bar">
      <span class="small">行（含表头）</span>
      <input id="tblRows" type="number" value="4" min="1" max="30" style="width:80px">
      <span class="small">列</span>
      <input id="tblCols" type="number" value="2" min="1" max="10" style="width:80px">
      <button class="btn-primary" onclick="doInsertTable()">插入</button>
      <button onclick="closeModal()">取消</button>
    </div>
    <div class="small" style="margin-top:10px">插入后直接在格子里打字（第一行是表头）。
      要调版式：把光标放进表格，用工具栏的<b>＋行/－行/＋列/－列</b>改形状，
      用<b>合并→</b>做跨列（如整行标题、标签+内容），满意后点<b>表格美化</b>一键统一样式。</div>`;
  document.getElementById('modal').classList.add('on');
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
  const cell = _cellAtCaret(id);
  if (!cell){ toast('先把光标放到表格里的某个格子里','warn'); return; }
  const row = cell.parentNode;
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
  toast('已加一行','ok');
}
function tableDelRow(id){
  const cell = _cellAtCaret(id);
  if (!cell){ toast('先把光标放到表格里','warn'); return; }
  const row = cell.parentNode;
  const tbl = row.closest('table');
  if (tbl.rows.length <= 1){ toast('只剩一行了，不能删；要整张删用「删除表格」','warn'); return; }
  const prev = row.previousElementSibling || row.nextElementSibling;
  row.remove();
  if (prev) _placeCaret(prev.cells[0]);
  toast('已删一行','ok');
}
function tableAddCol(id){
  const cell = _cellAtCaret(id);
  if (!cell){ toast('先把光标放到表格里','warn'); return; }
  const tbl = cell.closest('table');
  [...tbl.rows].forEach(r => {
    const isHead = r.cells[0] && r.cells[0].tagName === 'TH';
    const c = document.createElement(isHead ? 'th' : 'td');
    c.innerHTML = '&nbsp;';
    r.appendChild(c);
  });
  toast('已在末尾加一列（新列在最后一列，把它拖到想要的位置即可）','ok');
}
function tableDelCol(id){
  const cell = _cellAtCaret(id);
  if (!cell){ toast('先把光标放到表格里','warn'); return; }
  const tbl = cell.closest('table');
  if (_colCount(tbl) <= 1){ toast('只剩一列了，不能删','warn'); return; }
  const idx = [...cell.parentNode.cells].indexOf(cell);
  [...tbl.rows].forEach(r => { if (r.cells[idx]) r.cells[idx].remove(); });
  toast('已删一列','ok');
}
/* 与右格合并：版式里的"跨列"就是靠它做出来的（整行标题、标签+内容） */
function tableMergeRight(id){
  const cell = _cellAtCaret(id);
  if (!cell){ toast('先把光标放到要合并的格子里','warn'); return; }
  const next = cell.nextElementSibling;
  if (!next){ toast('右边没有格子了（已经是这一行最后一个）','warn'); return; }
  const a = parseInt(cell.getAttribute('colspan'), 10) || 1;
  const b = parseInt(next.getAttribute('colspan'), 10) || 1;
  cell.setAttribute('colspan', a + b);
  cell.setAttribute('rowspan', cell.getAttribute('rowspan') || 1);
  if ((cell.innerHTML || '').trim() === '') cell.innerHTML = next.innerHTML;
  next.remove();
  _placeCaret(cell);
  toast('已与右边合并（跨 ' + (a + b) + ' 列）','ok');
}
function tableSplit(id){
  const cell = _cellAtCaret(id);
  if (!cell){ toast('先把光标放到要拆的格子里','warn'); return; }
  const cs = parseInt(cell.getAttribute('colspan'), 10) || 1;
  if (cs <= 1){ toast('这个格子没有跨列，不需要拆','warn'); return; }
  cell.setAttribute('colspan', cs - 1);
  const nc = document.createElement(cell.tagName === 'TH' ? 'th' : 'td');
  nc.innerHTML = '&nbsp;';
  cell.parentNode.insertBefore(nc, cell.nextSibling);
  _placeCaret(nc);
  toast('已拆出一格（跨列 ' + cs + ' → ' + (cs - 1) + '）','ok');
}
function tableAlign(id, align){
  const cell = _cellAtCaret(id);
  if (!cell){ toast('先把光标放到表格里','warn'); return; }
  cell.style.textAlign = align;
  toast('该单元格已' + (align === 'center' ? '居中' : align === 'right' ? '右对齐' : '左对齐'),
        'ok');
}
/* 一键美化：统一边框 / 表头底色 / 行高 / 垂直居中。
   为什么要有它：手工给每个格子调样式既费劲又不一致；这里给一套克制的公文体，
   一次点好，之后还能单独改某个格子。样式写成**内联**——邮件客户端只认内联。 */
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
    tbl.setAttribute('style', 'border-collapse:collapse;width:100%;font-size:13.5px;'
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
function saveCurrentAsTemplate(){
  if (!editorBody('mailEditor').trim()){ toast('正文是空的，先把内容写好','warn'); return; }
  setModalTitle('把当前正文存为模板');
  document.getElementById('mBody').innerHTML = `
    <div class="kv">
      <div class="k">模板名</div><div><input id="tplSaveName" style="width:60%"
        placeholder="如：面试邀请（含表格）"></div>
      <div class="k">场景</div><div><input id="tplSaveScene" style="width:40%"
        value="初面邀约"></div>
    </div>
    <div class="small" style="margin-top:10px">会连主题一起保存。
      以后想让它自动填候选人信息，就把对应位置改成变量（如 <code>{姓名}</code>、
      <code>{毕业院校}</code>、<code>{面试时间}</code>），生成草稿时自动替换。</div>
    <div class="bar" style="margin-top:12px">
      <button class="btn-primary" onclick="doSaveAsTemplate()">保存模板</button>
      <button onclick="closeModal()">取消</button>
      <span class="small" id="tplSaveMsg"></span></div>`;
  document.getElementById('modal').classList.add('on');
}

async function doSaveAsTemplate(){
  const name = (document.getElementById('tplSaveName').value || '').trim();
  const msg = document.getElementById('tplSaveMsg');
  if (!name){ toast('请填一个模板名','warn'); return; }
  msg.textContent = '保存中…';
  beautifyTable('mailEditor', true);            // 同样先统一表格样式再存
  const r = await api('/api/mail/templates', {method:'POST', body:JSON.stringify({
    name: name,
    scene: (document.getElementById('tplSaveScene').value || '').trim() || '其他通知',
    subject: document.getElementById('mSubject').value || '',
    body: editorBody('mailEditor')})});
  if (r.__http_error || r.error){ msg.textContent = r.detail || r.error || '保存失败'; return; }
  closeModal();
  toast(r.note || ('模板「' + name + '」已保存'), 'ok');
  await viewMail();                     // 重新载入模板下拉，马上就能选到
}

/* 粘贴处理：从邮箱/Word 复制过来的 HTML 里带一堆内联样式与 class，
   直接落进正文会让"编辑器里看到的"和"发出去的"不一致（我们发送时会做最小清洗）。
   这里只保留结构（表格/段落/加粗等）与文字，不让粘贴的内联样式污染版式。 */
/* 粘贴处理：从邮箱/Word 复制过来的 HTML 里带一堆内联样式与 class，
   直接落进正文会让"编辑器里看到的"和"发出去的"不一致（我们发送时会做最小清洗）。
   这里只保留结构（表格/段落/加粗等）与文字，不让粘贴的内联样式污染版式。

   **粘完自动美化**：剥掉原样式后表格是白底无边框的，看着像"没排版"；
   与其让 HR 自己发现"还要再点一次表格美化"，不如粘进来就按我们的公文风格统一好。
   粘贴是新建模板最常见的入口（从旧邮件/Word 里搬现成版式），这一步必须顺。 */
function onEditorPaste(ev){
  const host = ev.target && ev.target.closest ? ev.target.closest('.richeditor') : null;
  const hostId = host ? host.id : 'mailEditor';
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

async function genMailDraft(){
  const cid = document.getElementById('mCand').value;
  const tid = document.getElementById('mTpl').value;
  const msg = document.getElementById('mMiss');
  if (!cid){ toast('请先选择收件人','warn'); return; }
  msg.textContent = '生成中…';
  const r = await api('/api/mail/draft', {method:'POST', body:JSON.stringify({
    candidate_id: parseInt(cid,10), template_id: tid ? parseInt(tid,10) : null,
    runtime: mailRuntime()})});
  if (r.__http_error || r.error){ msg.textContent = r.detail || r.error || '生成失败'; return; }
  document.getElementById('mSubject').value = r.subject || '';
  // 草稿直接灌进所见即所得编辑器：模板里的 `| 列 | 列 |` 在这里已经变成真表格
  const ed = editorEl('mailEditor');
  if (ed) ed.innerHTML = r.body_html || '<p><br></p>';
  // 草稿接口回了实际收件人，以它为准（可能与选人时不同，比如取的是投递上的邮箱）
  if (r.to) setMailTo(r.to);
  msg.innerHTML = (r.missing || []).length
    ? `<span style="color:var(--bad)">有变量没取到值：${esc(r.missing.join('、'))}
       —— 正文里已标成【待填：xxx】，发送前请补上（填了对应变量再点一次生成也行）。</span>`
    : `<span style="color:var(--ok)">${esc(r.note||'')}</span>`;
}

async function checkSmtp(){
  const msg = document.getElementById('smtpMsg');
  msg.textContent = '检查中…';
  const r = await api('/api/mail/test-smtp', {method:'POST'});
  /* v1.25：hint 必须一起显示。
     原来失败时只渲染 r.detail||r.error，把后端写好的**解决办法**整段丢了，
     HR 只能看到一句 SSLError —— 等于没提示。成功时也可能带 hint
     （自动纠正了端口/加密方式的时候）。 */
  const _hint = r.hint ? `<div class="small" style="margin-top:4px">${esc(r.hint)}</div>` : '';
  if (r.__http_error || r.error){
    msg.innerHTML = `<span style="color:var(--bad)">${esc(r.detail||r.error||'检查失败')}</span>${_hint}`;
    return;
  }
  msg.innerHTML = `<span style="color:var(--ok)">${esc(r.note||'配置可用')}</span>${_hint}`;
}

/* 收件人邮箱展示（v1.24.9）：统一走这里，醒目显示 + 同时写隐藏字段。
   为什么要有隐藏字段：原来 sendMailConfirm 靠
   `textContent.replace('将发往：','')` 反解邮箱——**改一次文案就发错人**，
   这个坑今天已经踩过（统计逻辑靠匹配文案字符串，静默失效）。
   现在"给人看的"和"给代码读的"分开：显示走 innerHTML，取值走 value。 */
function setMailTo(email, name){
  const box = document.getElementById('mTo');
  const hid = document.getElementById('mToVal');
  if (hid) hid.value = email || '';
  if (!box) return;
  if (!email){
    box.style.display = 'none';
    box.innerHTML = '';
    return;
  }
  box.style.display = 'block';
  box.innerHTML = `<div class="mailto">
      <span class="ml">收件人</span>
      <span class="mv">${esc(email)}</span>
      ${name ? `<span class="ml">（${esc(name)}）</span>` : ''}
    </div>`;
}

function mailToValue(){
  const hid = document.getElementById('mToVal');
  if (hid && hid.value) return hid.value;
  return '';   // 拿不到就空着——由 sendMailConfirm 如实报"没选收件人"，不猜
}

async function sendMailConfirm(){
  const to = mailToValue();
  const subject = document.getElementById('mSubject').value.trim();
  const html = editorBody('mailEditor');
  const plain = (editorEl('mailEditor') || {}).innerText || '';
  if (!subject && !plain.trim()){ toast('主题和正文都是空的','warn'); return; }
  if (!to || to.indexOf('@') < 0){ toast('收件人为空 —— 先选候选人','warn'); return; }
  const ok = await askConfirm({
    title: '确认发送这封邮件？',
    body: `<b>收件人：</b>${esc(to)}<br><b>主题：</b>${esc(subject || '(无主题)')}
           <br><br><span class="small">发出后无法撤回。确认无误再点「确认发送」。</span>`,
    okText: '确认发送', danger: false});
  if (!ok) return;
  const cid = document.getElementById('mCand').value;
  const tsel = document.getElementById('mTpl');
  const tname = tsel.value ? (tsel.selectedOptions[0].textContent || '') : '';
  const btn = document.getElementById('mailSendBtn');
  if (btn){ btn.disabled = true; btn.textContent = '发送中…'; }
  let r;
  try {
    r = await api('/api/mail/send', {method:'POST', body:JSON.stringify({
      to: to, subject: subject, body: plain, html: html,
      candidate_id: cid ? parseInt(cid,10) : null,
      template_name: tname})});
  } finally {
    if (btn){ btn.disabled = false; btn.textContent = '确认发送'; }
  }
  if (r.__http_error || r.error){
    mailSentBanner(false, r.detail || r.error || '发送失败');
    toast(r.detail || r.error || '发送失败','danger');
    return;
  }
  // v1.23.8：发信成功后**在编辑区就地留痕**，不只靠顶部 toast。
  // 原来只有 `toast('邮件已发送')`，而 #alerts 在页面顶部、发信操作在最下方，
  // toast 7 秒后还消失——用户盯着编辑器根本看不到，回过神又不知道发没发出去，
  // 于是会重复点。**发信是不可撤销的动作，必须留下"已发出去"的证据。**
  mailSentBanner(true, r.note || '邮件已发送',
    {to, subject, when: new Date(), from_header: r.from_header || ''});
  toast(r.note || '邮件已发送', 'ok');
}

/* 发信结果横幅：成功=绿、失败=红，都留在编辑区里（不自动消失），
   成功时把收件人/主题/时间/发件人显示名写清楚——这些是事后要核对的凭据。 */
function mailSentBanner(ok, msg, info){
  const host = document.getElementById('mailSentBox');
  if (!host) return;
  const from = (META.mailbox && META.mailbox.from_name) || '';
  if (ok){
    const when = (info && info.when) || new Date();
    const fh = (info && info.from_header) || '';
    host.innerHTML = `<div class="card" style="border-left:3px solid var(--ok);background:var(--ok-soft)">
      <b>✓ 已发送</b>
      <div class="small" style="margin-top:6px">
        收件人：${esc((info&&info.to)||'')}<br>
        主题：${esc((info&&info.subject)||'（无主题）')}<br>
        时间：${esc(String(when).slice(0,19))}
      </div>
      ${fh ? `<div class="small" style="margin-top:5px">本次发出的 From 头原文：
          <code style="word-break:break-all">${esc(fh)}</code></div>
        <div class="small" style="margin-top:2px">对方看到的名字与这里不一致时，
          是<b>邮箱服务商在服务器端覆盖了显示名</b>（QQ 邮箱会强制使用账号的「发件人名」）
          ——系统这边改不动，只能到对应邮箱的设置里改。</div>`
        : (from ? `<div class="small" style="margin-top:5px">发件人显示名：<b>${esc(from)}</b></div>` : '')}
      <div class="small" style="margin-top:6px">邮件已交给邮箱服务器发出，<b>无法撤回</b>。
        要改内容请重新编辑后再发一次（会再发一封）。</div>
    </div>`;
  } else {
    host.innerHTML = `<div class="card" style="border-left:3px solid var(--bad);background:var(--danger-soft)">
      <b>✗ 发送失败</b><div class="small" style="margin-top:4px">${esc(msg)}</div>
      <div class="small" style="margin-top:4px">内容还在编辑器里，改完可直接再点发送。</div>
    </div>`;
  }
  if (host.scrollIntoView) host.scrollIntoView({block:'nearest'});
}

/* ---- 模板编辑 ---- */
function tplFormHtml(t){
  const t_ = t || {};
  return `<div class="card" style="margin-top:10px;background:var(--surface-2)">
    <h2>${t_.id ? '编辑模板' : '新建模板'}</h2>
    <input type="hidden" id="tplId" value="${t_.id || ''}">
    <div class="kv">
      <div class="k">模板名</div><div><input id="tplName" style="width:60%" value="${esc(t_.name||'')}"
        placeholder="如：初面邀约"></div>
      <div class="k">场景</div><div><input id="tplScene" style="width:40%" value="${esc(t_.scene||'其他通知')}"></div>
      <div class="k">主题</div><div><input id="tplSubject" style="width:100%" value="${esc(t_.subject||'')}"
        placeholder="如：《{应聘岗位}》面试邀约 —— {姓名}"></div>
      <div class="k">正文</div><div>
        ${richEditorHtml('tplEditor', 260)}
        <div class="small" style="margin-top:6px">变量点一下就插到光标处（发送时自动替换成真实信息）：</div>
        <div class="bar" id="tplVarChips" style="flex-wrap:wrap;gap:4px 6px;margin-top:4px"></div>
        <div class="small" style="margin-top:8px;color:var(--ink-2)">
          <b>新建模板怎么做：</b>① 正文直接打字；
          ② 要表格点<b>「插入表格」</b>（插好即自动统一成公文样式）；
          ③ 也可以从<b>邮箱 / Word 复制现成表格直接粘进来</b>——粘完会自动统一边框、表头底色与行高；
          ④ 想微调：把光标放进单元格，用上面的 <b>＋行/－行/＋列/－列、合并→、对齐</b> 改；
          最后想整体再整齐一次，点<b>「表格美化」</b>。保存时会再自动统一一遍。</div>
      </div>
    </div>
    <div class="bar" style="margin-top:10px">
      <button class="btn-primary" onclick="saveTpl()">保存模板</button>
      <button onclick="document.getElementById('tplForm').innerHTML=''">取消</button>
      <span class="small" id="tplFormMsg"></span></div>
  </div>`;
}

/* 打开模板表单后要做的两件事：
   ① 把模板正文灌进可视化编辑器——**模板可能存的是纯文本或 HTML**，
      统一先经服务端转成 HTML 再显示，HR 看到的就是最终版式，不是源码；
   ② 渲染变量按钮：点一下插入 `{变量名}`，省得 HR 记变量名、也不会写错。 */
async function _tplFormReady(t){
  const chips = document.getElementById('tplVarChips');
  if (chips){
    const vars = (MAIL_VARS.builtin || []).concat(MAIL_VARS.runtime || []);
    chips.innerHTML = vars.map(v =>
      `<button class="mini" title="${esc(v.desc||'')}"
        onclick="insertVar('tplEditor','${esc(v.key)}')">${esc(v.key)}</button>`).join('')
      || '<span class="small">（变量清单未加载）</span>';
  }
  const ed = document.getElementById('tplEditor');
  if (!ed) return;
  const raw = (t && t.body) || '';
  if (!raw.trim()){ ed.innerHTML = '<p><br></p>'; return; }
  // 已经是 HTML 的模板原样显示；纯文本（含管道符表格）交给服务端转成 HTML。
  // 刻意不用正则判标签：ui.py 是普通 Python 字符串，正则里的反斜杠转义会被 Python
  // 先吃一遍（轻则报警告，重则把换行转义变成真换行、把注释断成代码）。
  // 用 indexOf 判标签最省事，也没有这层跨语言转义的坑。
  const HTML_TAGS = ['<table', '<tr', '<td', '<p>', '<p ', '<div', '<span',
                     '<b>', '<strong', '<br', '<ul', '<ol', '<li', '<h1', '<h2', '<h3'];
  const low = raw.toLowerCase();
  if (HTML_TAGS.some(t => low.indexOf(t) >= 0)){
    ed.innerHTML = raw;
    return;
  }
  const r = await api('/api/mail/preview', {method:'POST', body:JSON.stringify({body: raw})});
  ed.innerHTML = (r && r.html) ? r.html
    : ('<p style="white-space:pre-wrap">' + esc(raw) + '</p>');
}
function newTpl(){
  document.getElementById('tplForm').innerHTML = tplFormHtml(null);
  _tplFormReady(null);
}
function editTpl(tid){
  const t = MAIL_TPL.find(x => x.id === tid);
  document.getElementById('tplForm').innerHTML = tplFormHtml(t);
  _tplFormReady(t);
}
async function saveTpl(){
  const msg = document.getElementById('tplFormMsg');
  const id = document.getElementById('tplId').value;
  const payload = {
    id: id ? parseInt(id,10) : null,
    name: document.getElementById('tplName').value.trim(),
    scene: document.getElementById('tplScene').value.trim(),
    subject: document.getElementById('tplSubject').value,
    body: editorBody('tplEditor')
  };
  if (!payload.name){ msg.textContent = '模板名不能为空'; return; }
  // 保存前**自动统一表格样式**：新建/粘贴来的表格不必自己记得点「表格美化」，
  // 存下来的就是整齐版式（HR 仍可在编辑器里单独调某个格子）。
  const _tidied = beautifyTable('tplEditor', true);
  payload.body = editorBody('tplEditor');
  msg.textContent = '保存中…';
  const r = await api('/api/mail/templates', {method:'POST', body:JSON.stringify(payload)});
  if (r.__http_error || r.error){ msg.textContent = r.detail || r.error || '保存失败'; return; }
  toast('模板已保存' + (_tidied ? '（保存前已统一 ' + _tidied + ' 张表格的样式）' : ''), 'ok');
  await viewMail();
}
async function delTpl(tid, name){
  if (!await askConfirm({title:'删除模板？', danger:true, okText:'删除',
      body:`将删除模板「${esc(name)}」。已发出的邮件不受影响。`})) return;
  const r = await api('/api/mail/templates/'+tid+'/delete', {method:'POST'});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'删除失败','danger'); return; }
  toast('模板已删除','ok');
  await viewMail();
}

/* ------------------------------ 邮箱配置 ------------------------------ */
/* 收信配置（原「邮箱配置」页，已并入写邮件页）。
   IMAP 只读增量拉取：不删信、不改已读，收完即入库并自动去重。 */
async function renderMailCfgInto(boxId){
  const box = document.getElementById(boxId);
  if (!box) return;
  const c = await api('/api/mailbox/config');
  const attExt = (c.attachment_ext||[]).join(',');
  box.innerHTML = `
  <div class="panel">
    <h2>收信配置</h2>
    <div id="cfgWarn"></div>
    <div class="kv" style="margin-top:14px;grid-template-columns:170px 1fr">
      <div class="k">抓取模式</div><div>
        <select id="cfgMode">
          <option value="eml" ${c.mode==='eml'?'selected':''}>eml · 本地 .eml 目录（离线演练）</option>
          <option value="imap" ${c.mode==='imap'?'selected':''}>imap · 只读抓取招聘收件箱</option>
          <option value="off" ${c.mode==='off'?'selected':''}>off · 关闭抓取</option>
        </select>
        <div class="small">要真正收信必须选 imap。选 eml 时收取动作读的是本地目录，
          不会连邮箱——这是"配了没反应"最常见的原因。</div></div>
      <div class="k">本地邮件目录</div><div><input id="cfgEmlDir" value="${esc(c.eml_dir||'')}" style="width:340px" placeholder="data/mail_in"></div>
      <div class="k">本地简历文件夹</div><div><input id="cfgFolderDir" value="${esc(c.folder_dir||'')}" style="width:340px"
        placeholder="data/resumes 或 /Users/you/简历">
        <div class="small">「导入与来源」页从这里的文件建档。可填绝对路径（如外接盘或共享目录）。</div></div>
      <div class="k">服务商预设</div><div class="bar">
        <select id="cfgPreset" onchange="applyCfgPreset()"><option value="">（选择后自动填服务器/端口/SSL）</option></select>
        <span class="small">国内邮箱需在邮箱里开启 IMAP 并生成<b>授权码</b>，口令栏填授权码，不是登录密码。</span></div>
      <div class="k">IMAP 服务器</div><div><input id="cfgHost" value="${esc(c.imap_host||'')}" style="width:240px" placeholder="imap.example.com"></div>
      <div class="k">端口 / SSL</div><div class="bar">
        <input id="cfgPort" value="${c.imap_port==null?993:c.imap_port}" style="width:90px">
        <label style="display:flex;align-items:center;gap:5px"><input type="checkbox" id="cfgSsl" ${c.imap_ssl?'checked':''}> 使用 SSL/TLS</label></div>
      <div class="k">账号</div><div><input id="cfgUser" value="${esc(c.imap_user||'')}" style="width:260px" placeholder="jobs@example.cn"></div>
      <div class="k">授权码 / 口令</div><div><input id="cfgPass" type="password" style="width:260px"
        placeholder="${c.password_set?'已保存（留空则不修改）':'未设置'}">
        <span class="small">${c.password_set?'当前已保存口令（不回显）':'尚未保存口令'}</span></div>
      <div class="k">收件文件夹</div><div><input id="cfgFolder" value="${esc(c.imap_folder||'INBOX')}" style="width:180px"></div>
      <div class="k">附件白名单</div><div><input id="cfgExt" value="${esc(attExt)}" style="width:340px" placeholder=".pdf,.docx,.doc,.txt,.md"></div>
      <div class="k">单个附件上限</div><div class="bar">
        <input id="cfgMaxMb" value="${c.max_attachment_mb==null?20:c.max_attachment_mb}" style="width:90px">
        <span class="small">MB。邮件附件与「本地文件夹导入」<b>共用</b>这一条上限：
          超过的文件不导入、也不删（此前文件夹导入没有这条校验，一本书的 PDF 就是这么进来的）。</span></div>
      <div class="k">同岗重复天数</div><div><input id="cfgDays" value="${c.same_job_reapply_days==null?30:c.same_job_reapply_days}" style="width:90px">
        <span class="small">天内重复投递按「新版本」归档</span></div>
    </div>
    <div class="bar" style="margin-top:16px">
      <button class="btn-primary" onclick="saveMailCfg()">保存配置</button>
      <button onclick="testMailCfg()">测试 IMAP 连接</button>
      <button onclick="previewMail()">先看邮箱里有什么</button>
      <span class="small">收取简历的执行按钮在「人才库」页</span>
    </div>
    <div id="cfgOut"></div>
    <div id="mailPreview" style="margin-top:10px"></div>
  </div>`;
  loadCfgPresets();
  showCfgWarnings();
}

/* 发信配置（SMTP）：和收信并排放在「系统配置」。
   配好账号 ≠ 会自动发信——发送动作永远要人在「写邮件」页逐封点确认。 */
async function renderSmtpCfgInto(boxId){
  const box = document.getElementById(boxId);
  if (!box) return;
  const c = await api('/api/mail/smtp');
  const presets = c.presets || [];
  box.innerHTML = `
  <div class="panel">
    <h2>发信配置（SMTP）</h2>
    <div class="note">${esc(c.note||'')}</div>
    <div class="kv" style="margin-top:14px;grid-template-columns:170px 1fr">
      <div class="k">服务商预设</div><div class="bar">
        <select id="spPreset" onchange="applySmtpPreset()">
          <option value="">（选择后自动填服务器/端口/SSL）</option>
          ${presets.map(p=>`<option value="${esc(p.host)}|${p.port}|${p.ssl?1:0}">${esc(p.label)}</option>`).join('')}
        </select></div>
      <div class="k">SMTP 服务器</div><div><input id="spHost" value="${esc(c.host||'')}" style="width:240px" placeholder="smtp.163.com"></div>
      <div class="k">端口 / SSL</div><div class="bar">
        <input id="spPort" value="${c.port==null?465:c.port}" style="width:90px">
        <label style="display:flex;align-items:center;gap:5px"><input type="checkbox" id="spSsl" ${c.ssl?'checked':''}> 使用 SSL/TLS（465 端口勾它）</label></div>
      <div class="k">发信账号</div><div><input id="spUser" value="${esc(c.user||'')}" style="width:260px" placeholder="jobs@example.cn"></div>
      <div class="k">授权码</div><div><input id="spPwd" type="password" style="width:260px"
        placeholder="${c.password_set?'已保存（留空则不修改）':'未设置'}">
        <span class="small">${c.password_set?'当前已保存（不回显）':'尚未保存'}　留空 = 不修改</span></div>
      <div class="k">发件人显示名</div><div><input id="spFrom" value="${esc(c.from_name||'')}" style="width:260px"
        placeholder="西北有色金属研究院 人力资源部"></div>
    </div>
    <div class="bar" style="margin-top:16px">
      <button class="btn-primary" onclick="saveSmtp()">保存发信配置</button>
      <button onclick="checkSmtp()">检查发信配置（只测凭据，不发信）</button>
      <span id="smtpMsg" class="small"></span>
    </div>
  </div>`;
}

async function loadCfgPresets(){
  const sel = document.getElementById('cfgPreset');
  if (!sel) return;
  const r = await api('/api/mailbox/presets');
  sel.innerHTML = '<option value="">（选择后自动填服务器/端口/SSL）</option>'
    + (r.presets||[]).map(p=>`<option value="${esc(p.host)}|${p.port}|${p.ssl?1:0}">${esc(p.label)}</option>`).join('');
  sel.title = r.note||'';
}
function applyCfgPreset(){
  const sel = document.getElementById('cfgPreset');
  if (!sel || !sel.value) return;
  const [host, port, ssl] = sel.value.split('|');
  document.getElementById('cfgHost').value = host;
  document.getElementById('cfgPort').value = port;
  document.getElementById('cfgSsl').checked = (ssl === '1');
  if (document.getElementById('cfgMode').value !== 'imap'){
    document.getElementById('cfgMode').value = 'imap';
    toast('已填入 '+host+'，并把模式切到 imap（记得保存）','info');
  } else {
    toast('已填入 '+host+'（端口 '+port+'）','info');
  }
}
async function showCfgWarnings(){
  const host = document.getElementById('cfgWarn');
  if (!host) return;
  // 提醒规则由后端一处产出（后端也用它给保存响应加 warnings），界面只负责显示
  const c = await api('/api/mailbox/config');
  const ws = c.warnings || [];
  host.innerHTML = ws.length
    ? `<div class="warn" style="margin-top:10px">配置看起来还差几处，收取不会生效：<br>${ws.map(x=>'· '+esc(x)).join('<br>')}</div>` : '';
}
async function saveMailCfg(){
  const body = {
    mode: document.getElementById('cfgMode').value,
    eml_dir: document.getElementById('cfgEmlDir').value.trim(),
    folder_dir: document.getElementById('cfgFolderDir').value.trim(),
    imap_host: document.getElementById('cfgHost').value.trim(),
    imap_port: parseInt(document.getElementById('cfgPort').value)||null,
    imap_ssl: document.getElementById('cfgSsl').checked,
    imap_user: document.getElementById('cfgUser').value.trim(),
    imap_folder: document.getElementById('cfgFolder').value.trim()||'INBOX',
    attachment_ext: document.getElementById('cfgExt').value.split(',').map(s=>s.trim()).filter(Boolean),
    max_attachment_mb: parseInt(document.getElementById('cfgMaxMb').value)||null,
    same_job_reapply_days: parseInt(document.getElementById('cfgDays').value)||null,
  };
  const pass = document.getElementById('cfgPass').value;
  if(pass) body.password = pass;
  const r = await api('/api/mailbox/config',{method:'POST',body:JSON.stringify(body)});
  if(r.__http_error||r.error){toast(r.detail||r.error||'保存失败','danger');return;}
  const warn = r.warnings||[];
  toast('邮箱配置已保存'+(r.password_set?'（口令已更新）':'')+(warn.length?(' ｜ '+warn.join('；')):''),
        warn.length?'warn':'ok');
  document.getElementById('cfgOut').innerHTML = warn.length
    ? `<div class="warn" style="margin-top:10px">还有几处需要确认：<br>${warn.map(x=>'· '+esc(x)).join('<br>')}</div>`
    : '<div class="ok" style="margin-top:10px">配置看起来是完整可用的。</div>';
  META = await api('/api/meta');
  showCfgWarnings();
}
async function testMailCfg(){
  const body = {
    imap_host: document.getElementById('cfgHost').value.trim(),
    imap_port: parseInt(document.getElementById('cfgPort').value)||null,
    imap_ssl: document.getElementById('cfgSsl').checked,
    imap_user: document.getElementById('cfgUser').value.trim(),
    imap_folder: document.getElementById('cfgFolder').value.trim()||'INBOX',
  };
  const pass = document.getElementById('cfgPass').value;
  if(pass) body.password = pass;
  const r = await api('/api/mailbox/test',{method:'POST',body:JSON.stringify(body)});
  if(r.__http_error){toast(r.detail||r.error||'测试失败','danger');return;}
  if(r.ok){
    toast((r.message||'连接成功')+(r.next_step?(' ｜ '+r.next_step):''),'ok');
    document.getElementById('cfgOut').innerHTML =
      `<div class="ok" style="margin-top:10px">${esc(r.message||'连接成功')}
       ${r.next_step?('<div class="small" style="margin-top:4px">'+esc(r.next_step)+'</div>'):''}</div>`;
  } else {
    toast(r.error||'连接失败','danger');
    document.getElementById('cfgOut').innerHTML =
      `<div class="danger" style="margin-top:10px">${esc(r.error||'连接失败')}</div>`;
  }
}

/* ------------------------------ 系统说明 ------------------------------ */
// v1.21.3：**显示名与数据 key 必须分开**。
// 原来把中文显示名（"面试单位"）当成 key 传进 dictDel/dictAdd，
// 而数据表是 units/rooms/… → next['面试单位'] 是 undefined → 静默 return，
// 界面表现就是"× 和添加都是假的"（联系人走另一条路，所以只有它是好的）。
const DICT_KEYS = [['面试单位','units'], ['会议室','rooms'],
                   ['面试时段','slots'], ['面试方式','modes']];

function _dictRows(key, label, items){
  const cur = items || [];
  if (!cur.length) return '<div class="small" style="color:var(--ink-3)">（还没有内容，用下面的框添加）</div>';
  return `<div class="dict-list">${cur.map((x,i)=>`
    <div class="dict-row"><span>${esc(x)}</span>
      <b onclick="dictDel('${esc(key)}',${i})" title="点 × 从字典里去掉">×</b></div>`).join('')}</div>`;
}
function _dictList(key, label, items, ph){
  return `<div class="k" style="vertical-align:top">${esc(label)}</div><div>
      ${_dictRows(key, label, items)}
      <div class="dict-add">
        <input id="new_${esc(key)}" placeholder="${esc(ph||'输入新的一项，回车添加')}"
               onkeydown="if(event.key==='Enter'){dictAdd('${esc(key)}');}">
        <button class="mini" onclick="dictAdd('${esc(key)}')">添加</button>
      </div>
    </div>`;
}
function _dictPayload(){
  // 从各字典的当前列表读（不再是 textarea——那是兜底编辑区）
  return {
    units: (window._dictData.units||[]).slice(),
    rooms: (window._dictData.rooms||[]).slice(),
    slots: (window._dictData.slots||[]).slice(),
    modes: (window._dictData.modes||[]).slice(),
    contacts: (window._dictData.contacts||[]).slice(),
  };
}
async function dictSave(next, msg){
  const r = await api('/api/interview-dict', {method:'POST', body:JSON.stringify(next)});
  if (r.__http_error || r.error){ toast(r.detail || r.error || '保存失败','danger'); return; }
  window._dictData = r.dict || next;
  IDICT = window._dictData;
  renderDictBox();
  toast(msg || '已保存','ok');
}
async function dictDel(key, idx){
  const next = _dictPayload();
  if (!Array.isArray(next[key]) || idx >= next[key].length){
    // 不再静默 return——"点了没反应"最难查，出问题就说话
    toast('字典项定位失败（' + key + '#' + idx + '），请刷新页面重试','danger');
    return;
  }
  const gone = next[key][idx];
  next[key].splice(idx, 1);
  await dictSave(next, '已删除「' + gone + '」');
}
async function dictAdd(key){
  const inp = document.getElementById('new_'+key);
  const v = (inp && inp.value || '').trim();
  if (!v) { if (inp) inp.focus(); toast('先输入要添加的内容','warn'); return; }
  const next = _dictPayload();
  if (!Array.isArray(next[key])){
    toast('字典项定位失败（' + key + '），请刷新页面重试','danger');
    return;
  }
  if (next[key].indexOf(v) >= 0){ toast('已经有这一项了','warn'); return; }
  next[key] = next[key].concat([v]);
  await dictSave(next, '已添加「' + v + '」');
}
async function dictContactDel(idx){
  const next = _dictPayload();
  next.contacts.splice(idx, 1);
  await dictSave(next, '已删除该联系人');
}
async function dictContactAdd(){
  const d = (document.getElementById('new_c_dept')||{}).value || '';
  const n = (document.getElementById('new_c_name')||{}).value || '';
  const p = (document.getElementById('new_c_phone')||{}).value || '';
  if (!d.trim() && !n.trim()){ toast('至少填部门或姓名','warn'); return; }
  const next = _dictPayload();
  next.contacts.push({dept:d.trim(), name:n.trim(), phone:p.trim()});
  await dictSave(next, '已添加联系人');
}
function renderDictBox(){
  const box = document.getElementById('dictBox');
  if (!box){ return; }          // 页面还没渲染好——静默返回是本项目吃过亏的地方
  const d = IDICT || {};
  window._dictData = {units:d.units||[], rooms:d.rooms||[], slots:d.slots||[],
                      modes:d.modes||[], contacts:(d.contacts||[]).map(c=>({
                        dept:c.dept||'', name:c.name||'', phone:c.phone||''}))};
  const cs = window._dictData.contacts;
  box.innerHTML = `<div class="kv">
    ${_dictList('units', '面试单位', window._dictData.units, '如：材料研究中心')}
    ${_dictList('rooms', '会议室', window._dictData.rooms, '如：创新大楼1519会议室')}
    ${_dictList('slots', '面试时段', window._dictData.slots, '如：08:00-09:00')}
    ${_dictList('modes', '面试方式', window._dictData.modes, '如：现场面试')}
  </div>
  <div class="kv" style="margin-top:12px">
    <div class="k" style="vertical-align:top">联系人</div><div>
      ${cs.length ? `<div class="dict-list">${cs.map((c,i)=>`
        <div class="dict-row"><span>${esc(c.dept||'')}${c.dept&&c.name?' · ':''}${esc(c.name||'')}
          <span class="small">${esc(c.phone||'')}</span></span>
          <b onclick="dictContactDel(${i})" title="点 × 去掉这位联系人">×</b></div>`).join('')}</div>`
        : '<div class="small" style="color:var(--ink-3)">（还没有联系人）</div>'}
      <div class="dict-add">
        <input id="new_c_dept" placeholder="部门" style="width:26%">
        <input id="new_c_name" placeholder="姓名" style="width:20%">
        <input id="new_c_phone" placeholder="电话" style="width:30%">
        <button class="mini" onclick="dictContactAdd()">添加联系人</button>
      </div>
      <div class="small" style="margin-top:4px">联系人在写邮件时选人即可自动带出电话。</div>
    </div>
  </div>`;
}

async function saveDict(){
  // 现在增删都自动落盘了；这个按钮保留给"批量编辑"用——
  // 逐项增删不需要点它。
  await dictSave(_dictPayload(), '面试字典已保存');
}

async function viewSys(){
  const [pol, mc, dict] = await Promise.all([
    api('/api/policy'), api('/api/model-config'),
    api('/api/interview-dict').catch(()=>({}))]);
  IDICT = dict || {};
  const a = pol.access || {}, pii = a.pii_protection || {};
  document.getElementById('view').innerHTML = `
  <div class="panel"><h2>系统配置</h2>
  </div>
  <!--顺序原则（v1.15）：**需要填写/填错会出问题**的排最前（邮箱、SMTP、导入来源），
       可选与只读的往后（模板是写作素材，红线/数据存放只需读）。
       HR 的原话：「所有需要填写的配置的部分尽量往前面放」。 -->
  <div class="panel"><h2>面试字典（单位 / 会议室 / 时段 / 联系人）</h2>
    <div class="note">写邮件时的面试单位、地点、时段、联系人都从这份字典出。</div>
    <div id="dictBox"></div>
    <div class="bar" style="margin-top:8px">
      <button class="btn-primary" onclick="saveDict()">保存字典</button>
      <span class="small" id="dictMsg"></span>
    </div>
  </div>
  <div id="mailCfgBox"></div>
  <div id="smtpCfgBox"></div>
  <div id="importCfgBox"></div>
  <div id="tplMgrBox"></div>
  <div class="card"><h2>数据存放</h2>
    <div class="note">数据都在 <code>${esc((META.storage||{}).base||'resume-workbench')}/</code> 下：
      <div class="kv" style="margin-top:8px">
        <div class="k">主数据库</div><div><code>${esc((META.storage||{}).db||'data/workbench.db')}</code>（SQLite：候选人、投递、技能、审计、向量索引，联系方式在此库内 AES-GCM 加密）</div>
        <div class="k">简历原件</div><div><code>${esc((META.storage||{}).archive_dir||'data/archive')}</code>（入库后原件区，只增不改）</div>
        <div class="k">来源文件夹</div><div><code>${esc((META.storage||{}).resume_dir||'data/resumes')}</code>（待导入的简历，可在「导入与来源」改为任意本机目录）</div>
        <div class="k">回收目录</div><div><code>${esc((META.storage||{}).removed_dir||'data/removed')}</code>（界面上「删除」的来源文件移到这里，可恢复）</div>
        <div class="k">本地邮件</div><div><code>${esc((META.storage||{}).mail_dir||'data/mail_in')}</code>（eml 演练模式读取的目录）</div>
        <div class="k">备份</div><div><code>${esc((META.storage||{}).backup_dir||'data/backup')}</code></div>
        <div class="k">配置</div><div><code>config/</code>（模型、JD 尺子、档位规则、embedding、邮箱口令 imap.secret）</div>
      </div>
      <div class="small" style="margin-top:8px">备份方式：停服后整目录拷贝即可（重点 <code>data/</code> 与 <code>config/</code>）。</div>
    </div>
  </div>
  <div class="card"><h2>模型与密钥</h2>
    <div class="note">API Key 只以掩码显示。留空表示不修改。</div>
    <div class="kv" style="margin-top:8px">
      <div class="k">模型地址</div><div><input id="mcBaseUrl" style="width:90%" value="${esc(mc.base_url||'')}"
        placeholder="https://api.deepseek.com/v1"></div>
      <div class="k">模型名</div><div><input id="mcModel" style="width:60%" value="${esc(mc.model||'')}"
        placeholder="deepseek-chat / qwen2.5:14b"></div>
      <div class="k">API Key</div><div><input id="mcKey" type="password" style="width:60%"
        placeholder="${mc.key_set?('已配置（'+esc(mc.key_masked)+'），留空不修改'):'未配置，输入后保存'}">
        <span class="small">来源：${esc(mc.key_source||'—')}</span></div>
    </div>
    ${mc.env_override && mc.env_override.length
      ? `<div class="warn" style="margin-top:8px">环境变量 ${esc(mc.env_override.join('、'))} 已设置，优先于这里的文件值——改动可能被它盖过。</div>` : ''}
    <div class="bar" style="margin-top:10px">
      <button class="btn-primary" onclick="saveModelCfg()">保存模型配置</button>
      <span class="small">改完立即生效，无需重启；新地址/新模型是否可达见下方「运行环境」。</span>
    </div>
  </div>
  <div class="card"><h2>运行环境</h2>
    <div class="kv">
      <div class="k">对话模型</div><div>${(()=>{
        // v1.23：META.model 缺失时整页会崩成空白（实测：/api/meta 少了这个键就白屏）。
        // 这里兜一层 —— 一个信息块拿不到数据，不该拖垮整个系统配置页。
        const m = META.model || {};
        if (!m || m.model === undefined && !m.base_url){
          return '<span class="small">未获取到模型信息</span>';
        }
        return esc(m.model||'—') + ' · ' + (m.reachable?'服务可达':'服务不可达')
          + ' · ' + (m.model_installed?'模型已安装':'模型未安装')
          + (m.base_url?('<br><span class="small">'+esc(m.base_url)+'</span>'):'')
          + (m.error?('<br><span class="small">'+esc(m.error)+'</span>'):'');
      })()}</div>
      <div class="k">向量模型</div><div>${(()=>{
        // 与「对话模型」同一处防御：META.search 缺失时不该让整页崩成空白
        const se = META.search || {};
        let out = esc(se.model||'—') + ' · ' + (se.dim||0) + ' 维 · '
          + (se.reachable ? '服务可达'
             : '<span style="color:var(--ink-3)">未启用</span>（用本地哈希向量，零成本、不联网）')
          + ' · 已索引 ' + (se.indexed||0) + ' 人';
        if (se.index_model && se.index_model !== se.model){
          out += '<br><span class="small">索引实际使用 <b>' + esc(se.index_model) + '</b>。'
              + '这不是故障：哈希向量只匹配字面相近（"钛合金焊接"能命中、"金属连接"命中不了），'
              + '十几到几百人的库用技能检索 + 关键词基本够用；'
              + '真要语义检索再配一个本地向量模型（bge-m3 / gte-small），简历不出内网。</span>';
        }
        if (se.error) out += '<br><span class="small">' + esc(se.error) + '</span>';
        return out;
      })()}</div>
      <div class="k">解析能力</div><div>PyMuPDF ${META.parse.pymupdf?'':'✗'} ·
        MarkItDown ${META.parse.markitdown?'':'✗'} · OCR ${META.parse.ocr?'':'✗（图片简历将标『简历读不出』）'}</div>
      <div class="k">邮箱接入</div><div>模式 ${esc(META.mailbox.mode||'—')} ·
        只读 ${META.mailbox.readonly?'':'✗'} · 附件白名单 ${esc((META.mailbox.attachment_ext||[]).join(' '))}
        · 单个附件上限 ${META.mailbox.max_attachment_mb==null?20:META.mailbox.max_attachment_mb} MB
        · 同岗重复投递归并为新版本（${META.mailbox.same_job_reapply_days} 天内）</div>
      <div class="k">岗位</div><div>${(META.jobs||[]).length} 个（含已停用）</div>
    </div>
  </div>`;
  renderMailCfgInto('mailCfgBox');       // 收信（IMAP）
  renderSmtpCfgInto('smtpCfgBox');       // 发信（SMTP）
  renderTplMgrInto('tplMgrBox');         // 邮件模板管理
  renderImportCfgInto('importCfgBox');   // 来源目录 / 文件清单 / 历史导入记录
  renderDictBox();   // 必须在 innerHTML 之后调用

}

// 保存模型配置（v1.7.6）：地址/模型名直接下发，Key 留空 = 不修改；
// 后端写 config/model.json + secrets.json（0600）并写审计，保存即生效。
async function saveModelCfg(){
  const bu = document.getElementById('mcBaseUrl').value.trim();
  const md = document.getElementById('mcModel').value.trim();
  const ak = document.getElementById('mcKey').value;
  if (!bu){ toast('模型地址不能为空（例如 https://api.deepseek.com/v1）','warn'); return; }
  const r = await api('/api/model-config', {method:'POST',
    body:JSON.stringify({base_url:bu, model:md, api_key:ak})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'保存失败','danger'); return; }
  document.getElementById('mcKey').value = '';
  toast(r.note||'已保存','ok');
  META = await api('/api/meta');   // 新配置的可达状态要反映到「运行环境」
  refresh();
}
// 扩展机制信息（学科目录 + 领域包）：只读展示，让人知道"加新行业"的入口在哪。
// 刻意不在这里放"一键导入"：导入会改写全院共用的技能本体，属于口径级动作，
// 走命令行 `cli.py import-domain` 或接口的"先预演再落盘"两步，比在设置页点一下更稳。

boot();
</script></body></html>"""


# 界面版本戳：哈希的是**不含戳值本身**的模板（模板里那个占位符是固定的），
# 所以它是确定的、且模板一变就变。用途是让验收脚本能自证"验的是当前这版前端"——
# 单页应用整份 HTML/JS 是一张文档，浏览器缓存、服务未重启这两种情况
# 都会让验收**静默地跑上一版代码并全绿**，光靠"看见按钮了"证明不了这一点。
_UI_BUILD = hashlib.sha1(_PAGE.encode("utf-8")).hexdigest()[:12]


def ui_build() -> str:
    """当前**进程内**正在提供的前端版本戳（服务未重启时，它反映的是旧代码）。"""
    return _UI_BUILD


@lru_cache(maxsize=1)
def _logo_data_uri(size: int = 64) -> str:
    """侧栏品牌图标：把 icon_tray.png 缩到 64px 后内嵌成 data URI。

    为什么不直接 `<img src="/icon_tray.png">`：单页应用要离线可用、且不为一张
    图标新增静态路由；64px 的 PNG 转 base64 只有几 KB，随页面一次带走最省事。
    取不到图标（或没有 PIL）时返回空串，调用方退回原来的文字方块——**不显示裂图**。
    """
    import base64
    import io

    for p in (os.path.join(_BASE, "icon_tray.png"),
              os.path.join(_BASE, "_internal", "icon_tray.png")):
        if not os.path.exists(p):
            continue
        try:
            from PIL import Image

            with Image.open(p) as im:
                im = im.convert("RGBA").resize((size, size), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "PNG", optimize=True)
            raw = buf.getvalue()
        except Exception:                                   # noqa: BLE001
            with open(p, "rb") as fh:                       # 没有 PIL 就原样内嵌
                raw = fh.read()
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    return ""


def render_page(auth_enabled: bool = False) -> str:
    uri = _logo_data_uri()
    brand = (f'<img class="logo" alt="企业人才库智能体" src="{uri}">' if uri
             else '<span class="logo">才</span>')
    return (_PAGE
            .replace("__BRAND_LOGO__", brand)
            .replace("__UI_BUILD__", _UI_BUILD)
            .replace("__AUTH_ENABLED__", "true" if auth_enabled else "false"))
