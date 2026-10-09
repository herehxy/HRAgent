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
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    /* 纸与墨。HR 要求「页面不要底色」→ body 纯白，面与面靠**边框**区分，不靠底色 */
    --paper:#ffffff;        /* 页面底：纯白 */
    --surface:#ffffff;      /* 卡片/面板：也是白，靠 1px 边框立起来 */
    --surface-2:#f5f5f4;    /* 次级面（表头、代码块、hover） */
    --ink:#1c1b19;          /* 主文字 */
    --ink-2:#4a4844;        /* 次要文字（对白 ≥7:1） */
    --ink-3:#6b675f;        /* 弱化文字（对白 ≥4.6:1，原来 #86909c 只有 3.5:1 不达标） */
    --line:#e4e2dd;         /* 分隔线 */
    --line-2:#d3d0c9;
    /* 唯一强调色：**天蓝**（HR 定的）。面积控制在 5% 以内（Hallmark Gate 23）——
       只给「可点 / 当前 / 进行中」，卡片与 chip 一律中性。 */
    --accent:#3d8fd1;       /* 天蓝：用于边框、选中线、focus */
    --accent-ink:#16628f;   /* 深天蓝：用于文字（链接/按钮字），对白 ≥5:1 */
    --accent-soft:#e8f2fb; /* 浅天蓝：按钮底、选中底 */
    --focus:#16628f;        /* 焦点环 */
    /* 状态色只用于状态，不做装饰 */
    --ok:#2c6a45; --warn:#8a5a1b; --bad:#9c3226;
    --ok-soft:#e7f0e8; --warn-soft:#f7efe0; --bad-soft:#f8e9e5;
    /* 圆角：统一收敛 */
    --r-card:6px; --r-ctl:4px; --r-chip:3px;
  }

  /* Gate 34：窄屏不允许横向滚动。clip 而非 hidden——后者会破坏 sticky/fixed */
  html,body{overflow-x:clip}
  *{box-sizing:border-box}
  body{margin:0;background:var(--surface-2);color:var(--ink);
       font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;
       font-size:15px;line-height:1.7}
  /* 大屏适配（v1.7.1）：笔记本（<1600 宽）保持原样；24 寸及以上显示器整体放大，
     解决"内容挤在中间一圈留白、字号显小"的问题。
     为什么用 zoom 而不是逐个改字号：这套界面里几十处 px 字号（按钮 14、表格 14、
     说明文字 13、徽标 13……），逐个写媒体查询覆盖改动面大且必然漏；
     zoom 让字号、卡片、侧栏、弹窗**一起**按比例放大且布局随缩放重排，
     Chromium 与 Firefox 126+ 都支持。 */
  @media (min-width:1600px){ body{zoom:1.15} }
  @media (min-width:2000px){ body{zoom:1.3} }
  /* 飞书式布局：左侧固定导航 + 右侧内容区 */
  .layout{display:flex;min-height:100vh;align-items:stretch}
  .side{width:220px;flex:0 0 220px;background:#fff;border-right:1px solid var(--line);
    padding:20px 12px 14px;position:sticky;top:0;height:100vh;
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
  .navitem:hover{background:var(--surface-2);color:var(--ink)}
  .navitem.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:500}
  .sidefoot{font-size:12px;color:var(--ink-3);padding:10px 12px 0;border-top:1px solid var(--surface-2)}
  .main{flex:1;min-width:0;padding:24px 30px 60px}
  .main-inner{max-width:1240px;margin:0 auto}
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
  button:hover{color:var(--accent);border-color:#c0d0ff;background:#f5f8ff}
  button:disabled{opacity:.5;cursor:not-allowed}
  /* 主按钮：浅天蓝底 + 深天蓝字。HR 反馈过"深底浅字看不清"（国产字体渲染下更糊），
     浅底深字在低亮度屏上对比度更稳（WCAG 1A1）。 */
  .btn-primary{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-ink)}
  .btn-primary:hover{background:#dcecf9;border-color:var(--accent-ink);color:var(--accent-ink)}
  .btn-ok{background:var(--ok);border-color:var(--ok);color:var(--surface)}
  .btn-ok:hover{background:var(--ok);border-color:var(--ok);color:var(--surface)}
  .btn-danger{background:var(--surface);border-color:var(--line-2);color:var(--bad)}
  .btn-danger:hover{background:var(--bad-soft);border-color:var(--bad);color:var(--bad)}
  td button{padding:3px 10px;font-size:13px}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-card);padding:16px 18px;margin-bottom:12px}
  .panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-card);padding:18px 20px;margin-bottom:14px}
  .grid-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(124px,1fr));gap:0;margin:18px 0}
  .stat{padding:10px 14px;border-left:1px solid var(--line)}
  .stat:first-child{border-left:0;padding-left:2px}
  .stat .k{color:var(--ink-3);font-size:13px}
  .stat .v{font-size:26px;font-weight:600;margin-top:2px}
  .tabs{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 12px}
  .tab{padding:7px 15px;border:1px solid var(--line);border-radius:var(--r-ctl);background:var(--surface);cursor:pointer;font-size:14px;color:var(--ink-2)}
  .tab:hover{color:var(--accent);border-color:#c0d0ff}
  .tab.on{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-ink);font-weight:500}
  .chip{font-size:13px;padding:2px 9px;border-radius:var(--r-card);display:inline-block;margin:0 4px 4px 0}
  .badge{font-size:13px;padding:3px 10px;border-radius:var(--r-ctl);font-weight:500}
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
  .dict-tags{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:2px}
  .dict-tag{display:inline-flex;align-items:center;gap:4px;font-size:12px;
    padding:2px 6px 2px 8px;border:1px solid var(--line);border-radius:var(--r-chip);
    background:var(--surface-2);color:var(--ink-2)}
  .dict-tag b{cursor:pointer;color:var(--ink-3);font-weight:600;padding:0 2px}
  .dict-tag b:hover{color:var(--bad)}
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
  .avatar{width:44px;height:44px;border-radius:50%;display:flex;align-items:center;
    justify-content:center;font-size:17px;font-weight:600;flex:0 0 auto}
  .nm{font-size:18px;font-weight:600;letter-spacing:-.01em}
  .meta{font-size:13px;color:var(--ink-3);margin-top:3px}
  .acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px;line-height:1.5}
  .why{font-size:14px;color:var(--ink-2);margin-top:10px;line-height:1.75}
  .ev{font-size:13px;color:var(--ink-3);background:var(--surface-2);border-left:2px solid var(--line-2);
    padding:5px 9px;margin:4px 0;border-radius:0 4px 4px 0}
  .modal{position:fixed;inset:0;background:rgba(29,33,41,.45);display:none;align-items:center;
    justify-content:center;padding:22px;z-index:20}
  .modal.on{display:flex}
  .sheet{background:var(--surface);border-radius:var(--r-card);max-width:920px;width:100%;max-height:88vh;
    overflow:auto;padding:22px 24px}
  pre{white-space:pre-wrap;word-break:break-word;font-size:14px;color:var(--ink-2);line-height:1.8;
    background:var(--surface-2);padding:14px;border-radius:var(--r-ctl);margin:0}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--surface-2);vertical-align:top}
  th{color:var(--ink-3);font-weight:500;background:#fafbfc}
  .note{color:var(--ink-3);font-size:13px;line-height:1.75}
  .warn{background:var(--warn-soft);border:1px solid #ffe4ba;color:var(--warn);border-radius:var(--r-ctl);padding:10px 13px;font-size:14px}
  .info{background:var(--accent-soft);border:1px solid #d3e0ff;color:var(--accent-ink);border-radius:var(--r-ctl);padding:10px 13px;font-size:14px}
  .danger{background:var(--bad-soft);border:1px solid #ffd2c8;color:var(--bad);border-radius:var(--r-ctl);padding:10px 13px;font-size:14px}
  .ok{background:var(--ok-soft);border:1px solid #c9f2cd;color:var(--ok);border-radius:var(--r-ctl);padding:10px 13px;font-size:14px}
  .chatlog{margin-top:12px;max-height:440px;overflow:auto;display:flex;flex-direction:column;gap:9px}
  .msg{font-size:15px;line-height:1.75;padding:10px 13px;border-radius:var(--r-card);white-space:pre-wrap}
  .msg.user{background:var(--accent-soft);align-self:flex-end;max-width:78%}
  .msg.assistant{background:var(--surface-2);max-width:94%}
  .trace{font-size:13px;color:var(--ink-3);margin-top:8px;border-top:1px dashed var(--line);padding-top:8px}
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
  .contact a{color:var(--accent-ink);text-decoration:none;border-bottom:1px dashed #b8ccff}
  .contact a:hover{color:#0e42d2;border-bottom-style:solid}
  button.mini{font-size:12px;padding:1px 7px;margin-left:6px;border-radius:var(--r-ctl)}
  .srcbox{background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-ctl);padding:10px 12px;font-size:14px}
  .srcbox code{background:#eef1f6;padding:1px 6px;border-radius:5px;font-size:13px}
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
  .richeditor th{background:#f2f4f7;font-weight:600}
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
    <button onclick="closeModal()">关闭</button></div>
    <div id="mBody"></div></div>
</div>

<script>
const AUTH_ENABLED = __AUTH_ENABLED__;
// 前端版本戳（与 <meta name="tp-build">、/api/meta 的 ui_build 同源）：
// 验收脚本用它确认"页面里跑的就是服务端当前这版"，防止静默地验了缓存里的旧代码。
const UI_BUILD = '__UI_BUILD__';
const TIER_LABELS = {A:'优先面试',B:'建议面试',C:'储备',D:'暂不匹配当前岗位'};
const TIER_COLOR = {A:['var(--ok)','var(--ok-soft)'],B:['var(--accent)','var(--accent-soft)'],
                    C:['var(--warn)','var(--warn-soft)'],D:['var(--ink-3)','var(--surface-2)']};
const STAGES = ['新投递','已联系','初面','复面','待offer','已入职','已结束'];

let META = null;
/** 搜索模式：kw=关键词（字面）｜sem=语义（按意思）。
 *  v1.14：语义检索的函数一直存在但界面上没有入口（孤儿函数），HR 反馈"没有语义检索入口"，
 *  这里把它接回同一个搜索栏。语义走的是向量检索，**只用于找人，不参与档位判定**。 */
let SEARCH_MODE = 'kw';
let _semDegraded = false;   // 语义检索是否正在降级（提示只在真正用到时才出现）
let TOKEN = localStorage.getItem('tp_token') || '';
let VIEW = 'pool', TAB = 'ALL', KW = '', CHAT = [], ITEMS = [], CUR = null, LAST_INGEST = null;
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
  try { body = await r.json(); } catch(e) { body = {error:'响应不是 JSON'}; }
  if (!r.ok) return Object.assign({__http_error:r.status}, body||{});
  return body;
}
function toast(msg, kind){
  const d = document.createElement('div');
  d.className = kind || 'info';
  d.style.margin = '0 0 10px';
  d.textContent = msg;
  const host = document.getElementById('alerts');
  host.prepend(d);
  setTimeout(()=>d.remove(), 7000);
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
  if (su) su.textContent = META.session.mode === 'local'
    ? '本机模式 · 单 HR' : ('登录：' + META.session.username);
  const h = readHash();
  if (h) VIEW = VIEWS.some(x=>x[0]===h) ? h : 'pool';
  window.addEventListener('hashchange', () => go(readHash(), true));
  renderTabs();
  await refresh();
}

// 侧栏顺序（v1.15）：写邮件提到人才库之后——它是日常最高频动作之一，
// 归档沉到靠后（一年用一次，不该占黄金位）。
const VIEWS = [['brief','今日待办'],['pool','人才库'],['mail','写邮件'],
               ['chat','智能助手'],['org','岗位管理'],['archive','归档'],
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
  renderTabs(); refresh();
}
function readHash(){
  return (location.hash||'').replace(/^#/,'');
}

async function refresh(){
  // 顶部统计卡片只在「今日待办」与「人才库」展示。
  // 其他页面放一排数字只是噪音：看岗位管理或写邮件时，那排总数帮不上任何忙。
  const showStats = (VIEW === 'brief' || VIEW === 'pool');
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
}

function renderStats(s){
  if (!s || s.__http_error) return;
  const t = s.tiers || {};
  document.getElementById('stats').innerHTML =
    card('候选人数', s.people, 'var(--ink)') + card('A 优先面试', t.A||0, 'var(--ok)') +
    card('B 建议面试', t.B||0, 'var(--accent)') + card('C 储备', t.C||0, 'var(--warn)') +
    card('D 暂不匹配', t.D||0, 'var(--ink-3)') +
    card('待 HR 确认', s.pending, 'var(--bad)') + card('待人工判读', s.needs_review, 'var(--warn)') +
    card('投递数', s.applications, 'var(--ink)') + card('简历附件', s.documents, 'var(--ink)') +
    card('待确认提案', s.proposals_pending, 'var(--bad)');
}
function card(k,v,c){ return `<div class="stat"><div class="k">${k}</div><div class="v" style="color:${c}">${v==null?'—':v}</div></div>`; }

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
      <div style="padding:10px 0;border-bottom:1px solid #f0f1f3">
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

/* 招聘对象身份：有工作经历显示年限；没有则显示毕业时间 + 应届/往届未就业。
   校招场景下"能不能投"看的是身份，不是一个年限数字。 */
function expBadge(x){
  const e = x.exp_display;
  if (!e) return (x.years_exp==null ? '—' : x.years_exp + ' 年');
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
      `自动分析生成中…（稍后刷新，或点「重算自动分析」）</div>`;
  }
  const src = ins.source === 'rule_fallback' ? '规则降级（模型不可用）'
            : ins.source === 'auto_profile'  ? '未归岗 · 简历画像'
            : ins.source === 'manual'        ? '手动重算' : '进门即分析';
  const ev = (ins.evidence||[]).slice(0,2)
    .map(e => `[${e.skill}] ${(e.quote||'').slice(0,28)}`).join(' ｜ ');
  return `<div style="margin-top:10px;background:#f2f7ff;border-left:3px solid var(--accent);
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

async function reanalyze(cid){
  const out = document.getElementById('out-'+cid);
  if (out) out.innerHTML = '<div class="note">重算中…</div>';
  const r = await api('/api/candidates/'+cid+'/reanalyze', {method:'POST'});
  if (r.__http_error || r.error){
    if (out) out.innerHTML = `<div class="danger">重算失败：${esc(r.detail||r.error||'')}</div>`;
    return;
  }
  toast(r.note || '分析已更新', 'ok');
  refresh();
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
                ['D','D 暂不匹配'],['REVIEW','待人工判读'],['UNCONFIRMED','未复核']];
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
    <div class="flexbetween">
      <div class="bar">
        <input id="kwBox" placeholder="${SEARCH_MODE==='sem'?'语义检索：用一句话描述你想找的人（如：会钛合金焊接的硕士）':'搜索姓名 / 院校 / 专业 / 技能'}"
               style="width:${SEARCH_MODE==='sem'?'360px':'280px'}" value="${esc(KW)}"
               onkeydown="if(event.key==='Enter'){doSearch()}">
        <select onchange="SEARCH_MODE=this.value;poolPageReset();refresh()"
                title="关键词=姓名/院校/专业/技能的字面匹配；语义=按意思找（简历措辞不同也能命中）">
          <option value="kw" ${SEARCH_MODE==='sem'?'':'selected'}>关键词</option>
          <option value="sem" ${SEARCH_MODE==='sem'?'selected':''}>语义</option>
        </select>
        <button class="btn-primary" onclick="doSearch()">搜索</button>
        <button onclick="KW='';GENDER='';EDU_MIN='';UNIV='';poolPageReset();refresh()">清空</button>
        ${gsel}${eduSel}${uniSel}${stageSel}
      </div>
      <div class="bar">
        <button onclick="doIngest('mailbox')">收取邮箱简历</button>
        <button onclick="doIngest('folder')">导入本地文件夹</button>
        <button onclick="document.getElementById('upFile').click()"
                title="手机拍的纸质简历照片、扫描件，直接选文件上传入库（不用先改配置目录）">上传简历照片/文件</button>
        <input id="upFile" type="file" multiple hidden
               accept=".jpg,.jpeg,.png,.bmp,.webp,.pdf,.docx,.doc,.txt"
               onchange="doUpload(this.files)">
        <button onclick="exportCsv()">导出 CSV</button>
      </div>
      <div class="bar" style="margin-top:4px">
        <span class="small">入库即分析</span>
        <label class="small" style="display:flex;align-items:center;gap:4px;cursor:pointer"
               title="开启：新简历入库后自动分析一次（每人约 2-8 秒、消耗模型额度）；关掉：入库不调模型，改由你按需批量补">
          <input type="checkbox" ${isw.enabled?'checked':''}
                 onchange="setAutoInsight(this.checked)"> ${isw.enabled?'开（进门就有判断）':'关（改为手动批量分析）'}
        </label>
        <button id="btnAnalyzePending" data-pending="${isw.pending}"
                onclick="analyzePendingBatch()" ${isw.pending?'':'disabled'}>
          ${isw.pending ? ('分析待分析的人（'+isw.pending+'）') : '无需补充分析'}
        </button>
      </div>
    </div>
    <div class="bar" style="margin-top:8px">
      <span class="small" title="按年使用：新一年开始时把旧简历整批收起（归档不等于删除，满 30 天才彻底清理，期间可随时取消）">批量归档</span>
      <button onclick="archiveBatch(null,true)">归档勾选的人</button>
      ${(()=>{
        // v1.20：原来是一个年份输入框（打字/上下箭头）——容易打错成 1、2、3，
        // 也不可能出现负数，但**不友好**。改成卡片式，一眼点选、默认当年。
        // v1.21：改成**下拉框**（上一版做成了卡片行，HR 要的是下拉）。
        // 默认当年，选项是"某年以前"（**不含该年**：后端是 year < before_year）；不会出现负数或乱值。
        const y = new Date().getFullYear();
        const ys = [];
        for (let i=0;i<4;i++) ys.push(y-i);
        ys.push(2019);
        return `<div class="bar" style="margin-top:6px">
          <select id="poolArchYear" style="width:200px" onchange="pickArchYear(this.value)">
            ${ys.map(v=>`<option value="${v}"${v===y?' selected':''}>${v} 年以前</option>`).join('')}
            ${ys.indexOf(y)<0?`<option value="${y}" selected>${y} 年以前</option>`:''}
          </select>
          <button id="btnArchByYear" class="btn-primary" onclick="archiveByYear()">
            归档 ${y} 年以前的投递</button>
          <span class="small">默认 ${y} 年（当年）。要归档更早的年在下拉里选</span>
        </div>`;
      })()}

    </div>
    ${gtip}
    ${eduWarn}
    <div class="tabs" style="margin-bottom:0">
      ${tabs.map(([k,l])=>`<div class="tab ${TAB===k?'on':''}" onclick="TAB='${k}';poolPageReset();refresh()">${l}${k==='ALL'?(' '+n.ALL):''}</div>`).join('')}
    </div>
  </div>
  ${pipeBoardHtml(p)}
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
  const rev = x.needs_review ? '<span class="chip" style="color:var(--warn);background:var(--warn-soft)">待人工判读</span>' : '';
  const stage = x.stage || '新投递';
  const tiers = ['A','B','C','D'].map(k=>`<option value="${k}" ${k===t?'selected':''}>${k} · ${TIER_LABELS[k]}</option>`).join('');
  const stages = STAGES.map(k=>`<option value="${k}" ${k===stage?'selected':''}>${k}</option>`).join('');
  const skillChips = (x.skills||[]).slice(0,12).map(s=>`<span class="chip chip-skill">${esc(s)}</span>`).join('');
  const contact = contactLine(x);
  const scoreTip = (!job && sug)
    ? ` title="档位按「建议岗位 · ${esc(sug.title)}」的 JD 判断：学历不达标判 D，其余由模型给出"` : '';
  return `<div class="card">
    <div class="row1">
      <input type="checkbox" class="pickChk" value="${x.id}" style="margin-right:10px">
      <div class="avatar" style="color:${fg};background:${bg}">${esc((x.name||'?').slice(0,1))}</div>
      <div style="flex:1;min-width:0">
        <div class="nm">${esc(x.name||'未识别')} ${rev} ${genderTag} ${jobTag}</div>
        <div class="meta">${eduBadge(x)} · ${expBadge(x)} ·
          ${esc(x.school||'—')}${uniTag(x.uni_tier)} · 阶段 ${esc(stage)} ·
          来源 ${esc(x.channel||'—')} · 投递 ${esc((x.applied_at||'').slice(0,10))}</div>
        <div class="meta" style="margin-top:2px"><b>联系方式</b>：${contact}</div>
      </div>
      <div class="badge"${scoreTip} style="color:${fg};background:${bg}">${t} · ${TIER_LABELS[t]}</div>
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
      <button onclick="reanalyze(${x.id})">重新分析</button>
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
  document.getElementById('mTitle').textContent = (d.name||'未识别') + ' · 完整档案';
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
        : '<span class="small bad-txt">原件路径缺失</span>';
      return `<tr><td>${esc(x.file_name)}<div class="small">${esc(x.mime||'')} ·
         ${x.size?Math.round(x.size/1024)+' KB':''}</div></td>
      <td>${esc(x.parse_engine||'—')}<div class="small">${x.text_len?x.text_len+' 字':''}</div></td>
      <td>${x.parse_ok?'<span class="ok-txt">已解析</span>':'<span class="warn-txt">待人工判读</span>'}</td>
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
      <div class="k">姓名 / 学历 / 学校 / 专业</div><div class="bar" style="flex-wrap:wrap">
        <input id="edName" value="${esc(d.name||'')}" style="width:120px" placeholder="姓名">
        <select id="edEdu" style="width:120px">
          ${['', '大专', '本科', '硕士', '博士'].map(v =>
            `<option value="${v}" ${(d.edu_level||'')===v?'selected':''}>${v||'（学历未识别）'}</option>`).join('')}
        </select>
        <input id="edSchool" value="${esc(d.school||'')}" style="width:170px" placeholder="学校">
        <input id="edMajor" value="${esc(d.major||'')}" style="width:170px" placeholder="专业">
      </div>
      <div class="k">联系方式</div><div class="bar" style="flex-wrap:wrap">
        <input id="edPhone" value="${esc(contactValue(d.phone)||'')}" style="width:170px" placeholder="电话">
        <input id="edEmail" value="${esc(contactValue(d.email)||'')}" style="width:220px" placeholder="邮箱">
        <button onclick="saveFields(${d.id})">保存更正</button>
        <span class="small">识别错/识别不出时在这里改，改前改后写入审计（电话/邮箱加密存储）；
          <b>技能与年限不在此处改</b>（必须来自简历原文与证据核对）</span></div>
      <div class="k">学历 / 身份</div><div>${eduBadge(d)} · ${expBadge(d)}</div>
      <div class="k">院校 / 专业</div><div>${esc(d.school||'—')} · ${esc(d.major||'—')}</div>
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
      <div class="k">性别</div><div>${(d.gender||'').trim()
        ? esc(d.gender) + ' <span class="small">（简历明写；不参与档位判定）</span>'
        : '<span class="small">简历未写性别（系统不做推断）</span>'}</div>
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
        · 投递 ${(d.applications||[]).length} 次 · 附件 ${(d.documents||[]).length} 份</div>
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
    <div class="spacer"></div><div style="font-weight:600;font-size:15px">操作审计</div>
    <table><thead><tr><th>时间</th><th>动作</th><th>变更前</th><th>变更后</th><th>操作人</th></tr></thead>
      <tbody>${(d.audit_snippet||[]).map(a=>`<tr><td>${esc(a.ts)}</td><td>${esc(a.action)}</td>
        <td class="small">${esc(a.before)}</td><td class="small">${esc(a.after)}</td>
        <td>${esc(a.operator)}</td></tr>`).join('')||'<tr><td colspan="5">—</td></tr>'}</tbody></table>
    <div class="spacer"></div><div style="font-weight:600;font-size:15px">简历原文</div>
    <pre>${esc(d.raw_text||'（无文本，需人工查看附件原件；原件已留档）')}</pre>`;
  document.getElementById('modal').classList.add('on');
}
function closeModal(){ document.getElementById('modal').classList.remove('on'); }

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
    phone: (document.getElementById('edPhone').value || '').trim(),
    email: (document.getElementById('edEmail').value || '').trim(),
  };
  const r = await api('/api/candidates/'+cid+'/rename',
                      {method:'POST', body:JSON.stringify(payload)});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'更正失败','danger'); return; }
  toast(r.note || '已更正', 'ok');
  closeModal(); refresh();
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
    document.getElementById('mTitle').textContent = o.title || '请确认';
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
  if (!arr.length){ toast('没有可上传的文件（支持图片 / PDF / Word / txt）','warn'); return; }
  let ok = 0, fail = 0;
  for (const f of arr){
    try{
      // 裸字节上传：文件名走 query。不引 multipart 依赖（打包产物里没有它）
      const r = await fetch('/api/upload?name=' + encodeURIComponent(f.name),
        {method:'POST', headers:{'Content-Type':'application/octet-stream'}, body:f});
      const j = await r.json();
      if (r.ok && !j.error){ ok++; } else { fail++; }
    }catch(e){ fail++; }
  }
  toast(ok + ' 份已入库' + (fail ? ('，'+fail+' 份失败') : ''), fail ? 'warn' : 'ok');
  refresh();
}

async function doIngest(source){
  const label = source==='mailbox' ? '邮箱' : '本地文件夹';
  toast('正在收取简历…（'+label+'）','info');
  const r = await api('/api/ingest', {method:'POST', body:JSON.stringify({source:source})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'收取失败','danger'); return; }
  const num = k => (r[k]==null?0:r[k]);
  const ix = r.index || {};
  // v1.13.4：入库即分析关着时，如实告诉 HR"这几个人没分析"——不能让人以为分析过了。
  // 静默入库最坏：HR 打开一看没有分析结果，以为功能坏了。
  if (num('insight_skipped') > 0){
    toast(`已入库，但有 ${num('insight_skipped')} 人未分析`
      + `（"入库即分析"当前是关的）——可在上方点「分析待分析的人」批量补上`,'warn');
  }
  toast(`完成：新增 ${num('added')}，新版本 ${num('merged_versions')}，`
    + `跳过重复 ${num('skipped_dup')}，无附件 ${num('no_attachment')}，`
    + `解析失败(仍入库) ${num('parse_failed')}，超限跳过 ${num('skipped_oversize')}，`
    + `异常 ${num('failed')}`
    + `｜源：${r.source_label||label}`
    + `｜索引：新增 ${ix.indexed==null?0:ix.indexed} 条（跳过 ${ix.skipped==null?0:ix.skipped} 条）`
    (r.failed || r.skipped_oversize) ? 'warn':'ok');
  LAST_INGEST = r;                       // 明细留在「导入与来源」页看
  await refresh();
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
  const head = ['姓名','性别','学历','工作年限','院校','专业','专业方向/技能概要','手机','邮箱',
                '对应岗位','投递渠道','投递时间'];
  const rows = list.map(x=>{
    const sug = x.job_suggestion || null;
    const job = x.job_title || (sug ? `建议：${sug.title}` : '待指定');
    const skills = (x.skills||[]).slice(0,8).join('、');
    return [x.name||'', x.gender||'', x.edu_level||'', x.years_exp==null?'':x.years_exp,
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
  const due = items.filter(x=>(x.archive||{}).days_left===0).length;
  document.getElementById('view').innerHTML = `
  <div class="panel"><h2>归档</h2>
    <div class="note">共 <b>${items.length}</b> 份已归档档案。归档不是删除：
      档案、投递、附件、审计全部保留，点「取消归档」即恢复到人才库与检索。
      <b>归档满 30 天后系统会自动彻底删除</b>（原件移入回收目录，审计保留）——
      所以想留的人请在到期前取消归档${due?`，当前有 <b>${due}</b> 份已到期`:''}。</div>
    <div class="bar" style="margin-top:10px">
      <button onclick="archiveBatch(null,false)">批量取消归档（勾选的人）</button>
      <span class="vdiv"></span>
      <input id="archYear" type="number" placeholder="年份，如 2026" style="width:150px">
      <button onclick="archiveByYear()">归档该年以前的投递</button>
      <span class="small">按最后一条投递的年份整批归档，适合"新一年开始、旧简历收起来"</span>
    </div></div>
  ${items.length ? items.map(x=>{
    const t = x.tier_effective || 'D';
    const [fg,bg] = TIER_COLOR[t] || TIER_COLOR.D;
    const am = x.archive || {};
    const left = am.days_left;
    const expiring = left!=null && left<=7;
    const expired = left===0;
    const leftTxt = left==null ? ''
      : (expired ? `<span class="chip" style="color:var(--bad);background:var(--bad-soft)">已满 30 天，可彻底删除</span>`
                 : `<span class="chip" style="color:${expiring?'var(--bad)':'var(--ink-2)'};background:${expiring?'var(--bad-soft)':'var(--surface-2)'}"
                      title="到期后自动彻底删除，原件移入回收目录">还有 ${left} 天彻底删除（${esc(am.purge_at||'')}）</span>`);
    return `<div class="card">
      <div class="row1">
        <input type="checkbox" class="archChk" value="${x.id}" style="margin-right:10px">
        <div class="avatar" style="color:${fg};background:${bg}">${esc((x.name||'?').slice(0,1))}</div>
        <div style="flex:1;min-width:0">
          <div class="nm">${esc(x.name||'未识别')} ${leftTxt}</div>
          <div class="meta">${eduBadge(x)} · ${expBadge(x)} ·
            ${esc(x.school||'—')} · 阶段 ${esc(x.stage||'新投递')} ·
            归档于 ${esc((x.archived_at||'').slice(0,16)||'—')}</div>
          <div class="meta" style="margin-top:2px"><b>联系方式</b>：${contactLine(x)}</div>
        </div>
        <div class="badge" style="color:${fg};background:${bg}">${t} · ${TIER_LABELS[t]}</div>
      </div>
      <div class="acts">
        <button onclick="showDetail(${x.id})">完整档案</button>
        <button class="btn-primary" onclick="archiveCandidate(${x.id},false)">取消归档</button>
        ${expired ? `<span class="vdiv"></span>
        <button class="btn-danger" onclick="purgeOne(${x.id})">彻底删除</button>` : ''}
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
      ? '归档后在「归档」页可见，满 30 天会被彻底删除（期间可随时取消归档）。继续？'
      : '取消归档后立即恢复在人才库与检索中展示。继续？'))) return;
  const r = await api('/api/candidates/archive-batch', {method:'POST',
    body:JSON.stringify({ids:list, archived:archived})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||verb+'失败','danger'); return; }
  toast(`已${verb} ${r.changed} 人${r.skipped?`（跳过 ${r.skipped} 人）`:''}`,'ok');
  refresh();
}
let ARCH_YEAR = new Date().getFullYear();   // v1.21：默认当年，下拉选择
function pickArchYear(v){
  const y = parseInt(v, 10);
  if (!y || y < 1990 || y > 2100) return;      // 挡掉明显不合理的输入
  ARCH_YEAR = y;
  const b = document.getElementById('btnArchByYear');
  if (b) b.textContent = '归档 ' + y + ' 年以前的投递';
}
async function archiveByYear(){
  const y = ARCH_YEAR;
  if (!await askConfirm(`把「最后一条投递早于 ${y} 年」的档案整批归档（当前还在人才库里的）。\\n\\n`
      + `归档后在「归档」页可见，满 30 天会被彻底删除。继续？`)) return;
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
async function analyzePendingBatch(){
  const btn = document.getElementById('btnAnalyzePending');
  if (!btn || btn.disabled) return;
  const total0 = parseInt((btn.dataset.pending || '0'), 10);
  btn.disabled = true;
  let guard = 0;
  try {
    for(;;){
      const r = await api('/api/insights/analyze-pending',
        {method:'POST', body:JSON.stringify({limit: 5})});
      if (r.__http_error || r.error){
        toast(r.detail || r.error || '分析失败','danger'); break;
      }
      const done = r.analyzed || 0, left = r.remaining || 0;
      btn.textContent = left ? ('分析中…已处理 ' + (total0 - left) + '/' + total0)
                             : '正在刷新…';
      if (r.note) toast(r.note, left ? 'info' : 'ok');
      if (!left || !done || ++guard > 40) break;   // guard：防止后端一直返回 left>0 时死循环
    }
  } finally {
    btn.disabled = false;
    refresh();
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
  document.getElementById('view').innerHTML = `  <div class="panel">
    <div class="flexbetween">
      <div><h2 style="margin:0">HR 助手（智能体）</h2>
        <div class="note">模型自己决定调用哪些工具；<b>写操作只生成待确认提案</b>，HR 点确认才生效。
          模型不可用时自动降级为规则应答，数据仍来自真实库。<br>
          <b>对话历史保存在本机数据库</b>：刷新页面、重启服务后仍在；只有点「清空」才会清空。
          清空是<b>软清空</b>——30 天内可一键「恢复对话」，到期后系统才自动清理底层运行记录
          （清空 / 恢复 / 清理三条痕迹永久保留在「提案与审计」中）。</div></div>
      <div class="small" style="text-align:right">
        工具 ${t.total||0} 个：读 ${(t.read||[]).length} / 算 ${(t.compute||[]).length}
        / 写(需确认) ${(t.write_requires_confirmation||[]).length}<br>
        禁用：${(t.disabled||[]).join('、')}<br>
        累计运行 ${(runs.cost||{}).runs||0} 次，平均 ${(runs.cost||{}).avg_latency_ms||0} ms
      </div>
    </div>
    <div id="chatRetention" class="note" style="display:none;margin-bottom:8px"></div>
    <div class="chatlog" id="chatLog"></div>
    <div class="bar" style="margin-top:10px">
      <input id="chatInput" placeholder="例如：帮我找做过真空熔铸、会 XRD 的人" style="flex:1;min-width:240px"
             onkeydown="if(event.key==='Enter'){askAgent()}">
      <button class="btn-primary" onclick="askAgent()">发送</button>
      <button id="chatRestoreBtn" onclick="restoreChat()" style="display:none"
              title="把刚清空的对话找回来。清空后 30 天内有效，到期系统自动清理底层记录">恢复对话</button>
      <button onclick="clearChat()" title="清空对话展示；30 天内可恢复，到期系统自动清理底层运行记录">清空</button>
    </div>
    <div class="bar" style="margin-top:8px">
      ${['人才库有多少人？各档位分布如何？',
         '帮我找做过真空熔铸、会 XRD 的候选人',
         '现在招聘管道各阶段有多少、有没有积压',
         '有哪些待确认的提案'].map(q=>
        `<button class="small" onclick="document.getElementById('chatInput').value='${esc(q)}';askAgent()">${esc(q)}</button>`).join('')}
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
    const d = pushMsg(m.role, m.content);
    if (m.role !== 'assistant') return;
    const bits = [];
    if (m.mode) bits.push('模式：'+m.mode);
    if (m.rounds) bits.push('轮数 '+m.rounds);
    if (m.latency_ms) bits.push(m.latency_ms+' ms');
    if (m.tokens) bits.push('tokens '+m.tokens);
    if ((m.tools||[]).length) bits.push('工具：'+m.tools.map(x=>x.tool).join(' → '));
    if (bits.length) d.insertAdjacentHTML('beforeend', '<div class="trace">'+esc(bits.join(' · '))+'</div>');
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
function pushMsg(role, text){
  const log = document.getElementById('chatLog');
  const d = document.createElement('div');
  d.className = 'msg ' + role; d.textContent = text;
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
  holder.textContent = r.answer || (r.detail||'(无回复)');
  let extra = '';
  if (r.trace && r.trace.length){
    extra += '<div class="trace">工具调用轨迹：' + r.trace.map(t=>
      '→ '+esc(t.tool)+'('+esc(JSON.stringify(t.args))+')'+(t.write?' <b>[写·需确认]</b>':'')
      + (t.disabled?' <b>[已拦截]</b>':'')).join('<br>') + '</div>';
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
const APPROVE = String.fromCharCode(97,112,112,114,111,118,101);   // approve
const REJECT  = String.fromCharCode(114,101,106,101,99,116);           // reject
function renderProposalCards(holder, proposals){
  const perms = (META && META.session && META.session.permissions) || [];
  const canConfirm = perms.indexOf('confirm') >= 0;
  let html = '';
  proposals.forEach(p => {
    const acts = canConfirm
      ? '<button class="btn-primary" onclick="decideProposal(' + p.proposal_id + ',' + APPROVE + ',this)">确认执行</button>'
        + '<button class="btn-danger" onclick="decideProposal(' + p.proposal_id + ',' + REJECT + ',this)">拒绝</button>'
      : '<span class="small">你没有「确认」权限，请到「提案与审计」页处理</span>';
    html += '<div class="card" style="background:var(--surface)8e6;margin-top:10px" id="prop-' + p.proposal_id + '">'
      + '<div style="font-weight:600">待确认提案 · ' + esc(p.tool || '')
        + '<span class="small">提案 #' + p.proposal_id + (p.risk ? (' · 风险 ' + esc(p.risk)) : '') + '</span></div>'
      + '<div style="margin-top:6px">' + esc(p.summary || '') + '</div>'
      + '<div class="bar" style="margin-top:8px">' + acts
        + '<span class="small">不点就不会生效——系统不会替你做决定</span></div></div>';
  });
  holder.insertAdjacentHTML('beforeend', html);
}
async function decideProposal(pid, decision, btn){
  const card = document.getElementById('prop-' + pid);
  const _q = decision === 'approve' ? '确认执行这条提案吗？\\n\\n它会真的改档案（写审计）。'
                                 : '拒绝这条提案？' + BS + BS + 'n' + BS + BS + 'n拒绝也会写入审计（谁在什么时候拒的）。';
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
    card.style.background = decision === 'approve' ? '#f0fbf1' : '#f5f5f5';
    card.innerHTML = '<div style="font-weight:600">'
      + (decision === 'approve' ? '已执行' : '已拒绝') + ' · 提案 #' + pid + '</div>'
      + '<div class="small" style="margin-top:4px">' + esc(r.note || '') + '</div>';
  }
  refresh();   // 提案卡片就地变成"已执行/已拒绝"，不再弹 toast
}

/* 智能体起草的邮件：渲染成卡片 + 一键送进邮件编辑器（v1.13.7）。
   为什么要有这个卡：智能体说"我无法发送邮件"时，HR 真正缺的是**能用的草稿**。
   这里把草稿原样亮出来，HR 可以直接复制，或点按钮进编辑器改完自己点发送——
   **智能体不发送，这是红线**。 */
function renderDraftCards(holder, drafts){
  let html = '';
  drafts.forEach((d, i) => {
    const miss = (d.missing_runtime||[]).length
      ? `<div class="warn-txt" style="margin-top:6px">还缺：${esc((d.missing_runtime||[]).join('、'))}（补上后可以在对话里说"补上 XX 再写一版"）</div>` : '';
    const noMail = !d.to
      ? `<div class="bad-txt" style="margin-top:6px">库里没有这个人的邮箱，无法直接发——请先在档案里补邮箱，或改成你手动转发</div>` : '';
    html += `<div class="card" style="background:var(--surface-2);margin-top:10px">
      <div style="font-weight:600">邮件草稿（未发送）· ${esc(d.name||'')}
        <span class="small">收件人：${esc(d.to || '（无邮箱）')}${d.job?(' · 岗位：'+esc(d.job)):''}</span></div>
      <div class="small" style="margin-top:6px">主题：${esc(d.subject||'')}</div>
      <div style="margin-top:6px;white-space:pre-wrap">${esc(d.body||'')}</div>
      ${miss}${noMail}
      <div class="bar" style="margin-top:8px">
        <button onclick="copyText(${JSON.stringify('')});toast('已复制主题与正文','ok')">复制正文</button>
        <button class="btn-primary" onclick="openDraftInMailer(${i}, DRAFTS[${i}])">在邮件编辑器中打开</button>
        <span class="small">发送仍需你在编辑器里亲自点「确认发送」</span>
      </div></div>`;
  });
  DRAFTS = drafts;
  holder.insertAdjacentHTML('beforeend', html);
}
let DRAFTS = [];
async function openDraftInMailer(idx, d){
  await viewMail();
  try {
    const sel = document.getElementById('mCand');
    if (sel && d.candidate_id) sel.value = String(d.candidate_id);
    if (typeof fillMailTo === 'function') fillMailTo();
    const sub = document.getElementById('mSubject');
    if (sub) sub.value = d.subject || '';
    const ed = document.getElementById('mailEditor');
    if (ed) ed.innerHTML = d.body_html || esc(d.body || '');
    const pv = document.getElementById('mailPreview');
    if (pv && d.body_html) pv.innerHTML = d.body_html;
    toast('已填入编辑器：确认内容后由你点「确认发送」','ok');
  } catch (e) { toast('打开编辑器失败：' + e, 'danger'); }
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
    <div class="note">收信（IMAP）配置在上方「收信配置」卡片；这里只管本地文件夹与导入结果，
      执行导入的按钮在「人才库」页。</div>
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
  document.getElementById('mTitle').textContent = '回收目录 · ' + (r.recycle_dir||'');
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
    <div class="note" style="margin-top:8px">JD 就是这个岗位的判断尺子：<b>学历门槛</b>由它决定（不达标判 D），
      <b>档位 A/B/C</b>由模型按它给出的要求判断。
      <b>改了 JD 只影响之后新入库的投递，已入库的档位不会被追溯改动</b>——
      想让已有投递也按新尺子重排，点该岗位的「重新分析」：
      先给差异预览，确认后才落库，且 <b>HR 已确认过的档位一概不动</b>。
      只有岗位名称必填；JD 留空的项沿用默认尺子。</div>
    <div class="jdform" style="margin-top:12px">
      <label>岗位名称 *</label>
      <input id="jobTitle" placeholder="如：工艺工程师">
      <label>必需技能</label>
      <input id="jobMust" placeholder="逗号分隔，如：真空熔铸, 钛合金">
      <label>加分技能</label>
      <input id="jobPref" placeholder="逗号分隔，如：XRD, 有限元仿真">
      <label>最低学历</label>
      <select id="jobEdu">
        <option value="">（不限 / 沿用默认）</option>
        <option>大专</option><option>本科</option><option>硕士</option><option>博士</option>
      </select>
      <label>最低年限</label>
      <input id="jobYears" type="number" min="0" placeholder="如：3" style="width:120px">
      <label>专业需求（学科名）</label>
      <input id="jobMajor" list="majorList" placeholder="逗号分隔，如：材料科学与工程, 凝聚态物理">
      <datalist id="majorList"></datalist>
      <label>职责说明</label>
      <textarea id="jobNote" rows="3" placeholder="岗位职责、必须说明的事项（自由文本）"></textarea>
    </div>
    <div class="note" style="margin-top:6px">
      <b>专业需求填「学科名」，不要填进「必需技能」。</b>
      技能栏里写「材料科学与工程」这类学科名，候选人的技能栏永远不会出现这几个字，
      结果是全员命中 0 项。学科名（材料科学与工程、凝聚态物理、会计学…）认别名，
      判不出会标「未识别」——<b>未识别不等于不满足</b>。
      专业只作提示，<b>不参与档位淘汰</b>（材料物理去做工艺是常态）。
    </div>
    <div class="bar" style="margin-top:10px">
      <button class="btn-primary" onclick="addJob()">新增岗位</button>
      <span class="small">技能按逗号 / 顿号 / 分号分隔都认。</span>
    </div>
    <div class="note" style="margin-top:8px">邮件标题里带上岗位名时，收件会自动归到对应岗位；不带则标「待指定」。</div>
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
function jdEditorHtml(jid, jd){
  const must = jd.must || {}, pref = jd.preferred || {};
  const eduOpts = ['','大专','本科','硕士','博士'].map(v =>
    `<option value="${v}" ${v===(must.education_min||'')?'selected':''}>${v||'（不限 / 沿用默认）'}</option>`).join('');
  return `<div class="note">JD 是判断尺子：<b>学历门槛</b>决定是否判 D，档位 A/B/C 由模型按它判断。<b>保存后只影响之后新入库的投递，已入库的档位保持不变。</b></div>
  <div class="jdform" style="margin-top:12px">
    <label>必需技能</label>
    <input id="ejMust" value="${esc((must.skills_required||[]).join(', '))}" placeholder="逗号分隔">
    <label>加分技能</label>
    <input id="ejPref" value="${esc((pref.skills||[]).join(', '))}" placeholder="逗号分隔">
    <label>最低学历</label><select id="ejEdu">${eduOpts}</select>
    <label>最低年限</label>
    <input id="ejYears" type="number" min="0" style="width:120px"
           value="${must.years_min==null?'':must.years_min}">
    <label>专业需求（学科名）</label>
    <input id="ejMajor" value="${esc((must.major_required||[]).join(', '))}"
           placeholder="逗号分隔，如：材料学, 仪器科学与技术">
    <label>职责说明</label>
    <textarea id="ejNote" rows="3">${esc(jd.note||'')}</textarea>
  </div>
  <div class="note" style="margin-top:6px">
    专业需求填<b>学科名</b>（认别名，如「材料学」≡「材料科学与工程」），
    <b>不要填进技能栏</b>。专业只作提示、不参与淘汰；判不出会标「未识别」——未识别不等于不满足。
  </div>
  <div class="bar" style="margin-top:12px">
    <button class="btn-primary" onclick="saveJd(${jid})">保存 JD</button>
    <button onclick="regradeJob(${jid})">改完重新分析</button>
    <button onclick="closeModal()">取消</button>
    <span class="small">清空某一项 = 该项不再作为门槛。</span>
  </div>
  <div class="note" style="margin-top:8px">保存只影响之后新入库的投递；要让已有投递也按新尺子重排，
    点「改完重新分析」——它会先给你看差异，确认后才落库。</div>
  <div id="ejOut"></div>`;
}
async function editJd(jid){
  const d = await api('/api/jobs/'+jid);
  if (d.__http_error){ toast(d.detail||'读取失败','danger'); return; }
  document.getElementById('mTitle').textContent = (d.job.title||'岗位') + ' · 岗位 JD';
  document.getElementById('mBody').innerHTML = jdEditorHtml(jid, d.jd||{});
  document.getElementById('modal').classList.add('on');
}
async function saveJd(jid){
  const yv = document.getElementById('ejYears').value;
  // 清空 = 用空值覆盖，而不是"没填就不改"：HR 主动删掉门槛要真的生效
  const body = {
    must_skills: splitSkills(document.getElementById('ejMust').value),
    preferred_skills: splitSkills(document.getElementById('ejPref').value),
    education_min: document.getElementById('ejEdu').value,
    years_min: yv==='' ? 0 : parseInt(yv),
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
  document.getElementById('mTitle').textContent = '重新分析 · 按当前 JD 重算已有投递';
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
  if(yv!=='') body.years_min = parseInt(yv);
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
  const [p, a] = await Promise.all([api('/api/proposals'), api('/api/audit?limit=40')]);
  const all = p.items || [];
  const auto = all.filter(x=>x.source==='agent_auto' && x.status==='待确认');
  const srcTag = x => x.source === 'agent_auto'
    ? '<span style="color:var(--accent-ink);font-weight:500">系统巡检</span>'
    : '<span class="small">对话产生</span>';
  box.innerHTML = `
  <div class="panel"><h2>待确认提案</h2>
    <div class="note">系统提出的写操作。<b>在你确认之前，人才库没有任何改动。</b>
      「系统巡检」是系统自己发现的（没人问它），走同一条确认流。</div>
    ${auto.length?`<div class="bar" style="margin-top:8px">
      <span class="small">其中 <b>${auto.length}</b> 条来自系统巡检</span>
      <button class="btn-ok" onclick="decideMany('approve')">全部确认执行</button>
      <button onclick="decideMany('reject')">全部拒绝</button></div>`:''}
    <div class="spacer"></div>
    <table><thead><tr><th>#</th><th>来源</th><th>类型</th><th>内容</th><th>风险</th><th>状态</th>
      <th>提交时间</th><th>操作</th></tr></thead>
      <tbody>${all.map(x=>`<tr>
        <td>${x.id}</td><td>${srcTag(x)}</td><td>${esc(x.tool)}</td><td>${esc(x.summary)}</td>
        <td>${esc(x.risk)}</td><td>${esc(x.status)}</td><td class="small">${esc(x.created_at||'')}</td>
        <td>${x.status==='待确认'?`<button class="btn-ok"
            onclick="decide(${x.id},'approve')">确认执行</button>
          <button class="btn-danger"
            onclick="decide(${x.id},'reject')">拒绝</button>`:'—'}</td>
        </tr>`).join('')||'<tr><td colspan="8">暂无提案</td></tr>'}</tbody></table>
  </div>
  <details style="margin-top:12px"><summary class="small" style="cursor:pointer;color:var(--accent-ink)">
    操作审计（最近 40 条）· 谁在何时看了谁的简历、改了什么档、确认了什么提案，全部留痕</summary>
    <table style="margin-top:8px"><thead><tr><th>时间</th><th>对象</th><th>动作</th><th>变更前</th><th>变更后</th>
      <th>操作人</th></tr></thead>
      <tbody>${(a.items||[]).map(r=>`<tr>
        <td class="small">${esc(r.ts)}</td><td>${esc(r.entity)}#${esc(r.entity_id)}</td>
        <td>${esc(r.action)}</td><td class="small">${esc(r.before)}</td>
        <td class="small">${esc(r.after)}</td><td>${esc(r.operator)}</td>
        </tr>`).join('')||'<tr><td colspan="6">—</td></tr>'}</tbody></table></details>`;
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
        <div class="note">技能召回走本体精确匹配（结果可解释、带原文证据）；
          语义召回用于说不清关键词的探索性需求。
          当前索引模型 <b>${esc(s.index_model||'未建立')}</b>，已索引 ${s.indexed||0}/${s.people||0} 人。
          导入新简历后会自动建立增量索引（只处理新增/变更的人），无需手动重建。</div></div>
      <div class="bar"><button onclick="go('sys')">导入与来源配置在系统配置 →</button>
        <button onclick="refresh()">刷新索引状态</button>
        <span class="badge" style="background:var(--surface-2);color:var(--ink-3)">${esc(s.model||'')}</span></div>
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
  if (SEARCH_MODE === 'sem') return doSemSearch();
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
    <div class="note" style="margin-top:8px">语义相似度是相对排序指标，不是匹配度评分，请勿直接当筛除依据。</div>`;
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
  const clist = cands_.map(c =>
    `<option value="${c.id}">${esc(c.name || ('未识别#'+c.id))} · ${esc(c.email || '无邮箱')}</option>`
  ).join('');
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
    <div class="note"><b>发送必须由你点确认</b>，系统不做自动发送。
      变量取不到值会标成 <code>【待填：xxx】</code>，不会静默留空。<br>发信账号：${conf.user
        ? `<b>${esc(conf.user)}</b>（${esc(conf.host)}:${conf.port}，${conf.ssl?'SSL':'STARTTLS'}）`
        : '<span style="color:var(--bad)">未配置</span>'}
      <button onclick="checkSmtp()" style="margin-left:6px">检查发信配置</button>
      <span id="smtpMsg" class="small"></span></div>
    <div class="small" style="margin-top:8px">收发信配置（邮箱账号、授权码、来源目录、模板管理）都在
      <a href="javascript:go('sys')" style="color:var(--accent-ink)">系统配置</a> 里。</div>
  </div>
  <div class="card"><h2>第一步 · 选人、选模板</h2>
    <div class="kv">
      <div class="k">收件人</div><div>
        <select id="mCand" style="width:55%" onchange="fillMailTo()">
          <option value="">（选择候选人）</option>${clist}</select>
        <span class="small" id="mTo"></span></div>
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
      <button class="btn-ok" onclick="sendMailConfirm()">确认发送</button>
      <button onclick="saveCurrentAsTemplate()">把当前正文存为模板</button>
      <span class="small">点击后会再确认一次收件人与主题；发送动作写入审计</span>
    </div>
  </div>
`;
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
    <div class="note">模板 = 固定措辞 + <code>{变量}</code>，系统只做替换。
      正文用下面的<b>可视化编辑器</b>排版（表格、加粗、合并单元格都能直接调），
      <b>不用看也不用写任何代码</b>；变量点一下插到光标处。
      发送时按 HTML 邮件发出，并自动附一份纯文本版本兼容不显示 HTML 的客户端。
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

function fillMailTo(){
  const sel = document.getElementById('mCand');
  const opt = sel.selectedOptions[0];
  const mail = opt ? (opt.textContent.split(' · ')[1] || '') : '';
  document.getElementById('mTo').textContent = sel.value ? ('将发往：' + mail) : '';
}

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
  document.getElementById('mTitle').textContent = '插入表格';
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
            + 'font-size:18px;font-weight:600;background:#f2f4f7');
        } else if (isHead || ri === 0){
          // 粘贴来的表格常全是 td（没有 th）：**首行一律当表头**，
          // 否则整张表一片白，看着就是"没排版"（保留原标签，不破坏 colspan 结构）
          c.setAttribute('style', `${CELL};text-align:center;font-weight:600;`
            + 'background:#f2f4f7');
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
  document.getElementById('mTitle').textContent = '把当前正文存为模板';
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
  if (r.to) document.getElementById('mTo').textContent = '将发往：' + r.to;
  msg.innerHTML = (r.missing || []).length
    ? `<span style="color:var(--bad)">有变量没取到值：${esc(r.missing.join('、'))}
       —— 正文里已标成【待填：xxx】，发送前请补上（填了对应变量再点一次生成也行）。</span>`
    : `<span style="color:var(--ok)">${esc(r.note||'')}</span>`;
}

async function checkSmtp(){
  const msg = document.getElementById('smtpMsg');
  msg.textContent = '检查中…';
  const r = await api('/api/mail/test-smtp', {method:'POST'});
  if (r.__http_error || r.error){
    msg.innerHTML = `<span style="color:var(--bad)">${esc(r.detail||r.error||'检查失败')}</span>`;
    return;
  }
  msg.innerHTML = `<span style="color:var(--ok)">${esc(r.note||'配置可用')}</span>`;
}

async function sendMailConfirm(){
  const to = (document.getElementById('mTo').textContent || '').replace('将发往：','').trim();
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
  const r = await api('/api/mail/send', {method:'POST', body:JSON.stringify({
    to: to, subject: subject, body: plain, html: html,
    candidate_id: cid ? parseInt(cid,10) : null,
    template_name: tname})});
  if (r.__http_error || r.error){ toast(r.detail || r.error || '发送失败','danger'); return; }
  toast(r.note || '邮件已发送','ok');
}

/* ---- 模板编辑 ---- */
function tplFormHtml(t){
  const t_ = t || {};
  return `<div class="card" style="margin-top:10px;background:#fafbfc">
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
    <div class="note">把投递到招聘邮箱的简历自动收进人才库。IMAP 只读增量拉取，不删信、不改已读。</div>
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
    <div class="note" style="margin-top:10px">口令只会写入本地 config/imap.secret（权限 0600），不会显示在界面或日志里，也不提交到版本库。</div>
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
    <div class="note">这里只配发信账号，<b>系统不会自动发任何一封</b>；真正发送要你在「写邮件」页
      逐封点确认（会二次确认收件人与主题，动作写入审计）。<br>${esc(c.note||'')}</div>
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
function _dictRows(name, items){
  // 逐项一行 + × 删除（点一下直接从字典里去掉并落盘）
  const cur = items || [];
  if (!cur.length) return '<div class="small" style="color:var(--ink-3)">（还没有内容，用下面的框添加）</div>';
  return `<div class="dict-list">${cur.map((x,i)=>`
    <div class="dict-row"><span>${esc(x)}</span>
      <b onclick="dictDel('${esc(name)}',${i})" title="点 × 从字典里去掉">×</b></div>`).join('')}</div>`;
}
function _dictList(name, items, ph){
  return `<div class="k" style="vertical-align:top">${esc(name)}</div><div>
      ${_dictRows(name, items)}
      <div class="dict-add">
        <input id="new_${esc(name)}" placeholder="${esc(ph||'输入新的一项，回车添加')}"
               onkeydown="if(event.key==='Enter'){dictAdd('${esc(name)}');}">
        <button class="mini" onclick="dictAdd('${esc(name)}')">添加</button>
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
async function dictDel(name, idx){
  const next = _dictPayload();
  if (!next[name] || idx >= next[name].length) return;
  const gone = next[name][idx];
  next[name].splice(idx, 1);
  await dictSave(next, '已删除「' + gone + '」');
}
async function dictAdd(name){
  const inp = document.getElementById('new_'+name);
  const v = (inp && inp.value || '').trim();
  if (!v) { if (inp) inp.focus(); return; }
  const next = _dictPayload();
  if ((next[name]||[]).indexOf(v) >= 0){ toast('已经有这一项了','warn'); return; }
  next[name] = (next[name]||[]).concat([v]);
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
    ${_dictList('面试单位', window._dictData.units, '如：材料研究中心')}
    ${_dictList('会议室', window._dictData.rooms, '如：创新大楼1519会议室')}
    ${_dictList('面试时段', window._dictData.slots, '如：08:00-09:00')}
    ${_dictList('面试方式', window._dictData.modes, '如：现场面试')}
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
    <div class="note">收发信邮箱、简历来源目录、邮件模板都在这里集中配置。
      导入动作在「人才库」页有按钮，这里只管配置。</div>
  </div>
  <!--顺序原则（v1.15）：**需要填写/填错会出问题**的排最前（邮箱、SMTP、导入来源），
       可选与只读的往后（模板是写作素材，红线/数据存放只需读）。
       HR 的原话：「所有需要填写的配置的部分尽量往前面放」。 -->
  <div class="panel"><h2>面试字典（单位 / 会议室 / 时段 / 联系人）</h2>
    <div class="note">写邮件时的「面试单位、面试地点、面试时段、联系人」都从这份字典出，
      可以直接在这里增删。内置了西北有色院本部各研究所与人事处/财务处/数字化中心，
      以及创新大楼 1519 会议室——<b>换成你们自己的单位只要改这里，不用改代码</b>。
      联系人填好后，写邮件时选人就会自动带出电话。</div>
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
  <div class="panel"><h2>红线（写死在设计里）</h2>
    ${(pol.red_lines||[]).map(x=>`<div class="ok" style="margin-bottom:6px">${esc(x)}</div>`).join('')}
  </div>
  <div class="card"><h2>数据存放</h2>
    <div class="note">全部数据都在应用目录（<code>${esc((META.storage||{}).base||'resume-workbench')}/</code>）下，不写系统目录、不上传外部：
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
  <div class="card"><h2>口径偏差（系统建议 vs HR 决定）</h2>
    <div class="note">统计"系统建议档位 vs 你实际定档"的偏差，用来判断分级口径要不要调。
      <b>只做统计观察，不修改任何权重</b>。</div>
    <div id="fbBox"><span class="small">加载中…</span></div>
  </div>
  <div class="card"><h2>模型与密钥</h2>
    <div class="note">API Key 只以掩码显示（<code>sk-****1234</code>），完整值不离开服务端；
      保存写 <code>config/secrets.json</code>（0600，不入库不提交），**保存即生效**（每次模型调用重新读取配置）；
      动作写入审计。留空 Key 表示不修改。</div>
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
      <div class="k">对话模型</div><div>${esc(META.model.model||'—')} ·
        ${META.model.reachable?'服务可达':'服务不可达'} · ${META.model.model_installed?'模型已安装':'模型未安装'}
        ${META.model.base_url?('<br><span class="small">'+esc(META.model.base_url)+'</span>'):''}
        ${META.model.error?('<br><span class="small">'+esc(META.model.error)+'</span>'):''}</div>
      <div class="k">向量模型</div><div>${esc(META.search.model||'—')} · ${META.search.dim||0} 维 ·
        ${META.search.reachable
          ? '服务可达'
          : '<span style="color:var(--ink-3)">未启用</span>（用本地哈希向量，零成本、不联网）'} ·
        已索引 ${META.search.indexed||0} 人
        ${(META.search.index_model && META.search.index_model !== META.search.model)
          ? ('<br><span class="small">索引实际使用 <b>'+esc(META.search.index_model)+'</b>。'
             + '这不是故障：哈希向量只匹配字面相近（"钛合金焊接"能命中、"金属连接"命中不了），'
             + '十几到几百人的库用技能检索 + 关键词基本够用；'
             + '真要语义检索再配一个本地向量模型（bge-m3 / gte-small），简历不出内网。</span>')
          : ''}
        ${META.search.error?('<br><span class="small">'+esc(META.search.error)+'</span>'):''}</div>
      <div class="k">解析能力</div><div>PyMuPDF ${META.parse.pymupdf?'':'✗'} ·
        MarkItDown ${META.parse.markitdown?'':'✗'} · OCR ${META.parse.ocr?'':'✗（图片简历将标『待人工判读』）'}</div>
      <div class="k">邮箱接入</div><div>模式 ${esc(META.mailbox.mode||'—')} ·
        只读 ${META.mailbox.readonly?'':'✗'} · 附件白名单 ${esc((META.mailbox.attachment_ext||[]).join(' '))}
        · 单个附件上限 ${META.mailbox.max_attachment_mb==null?20:META.mailbox.max_attachment_mb} MB
        · 同岗重复投递归并为新版本（${META.mailbox.same_job_reapply_days} 天内）</div>
      <div class="k">岗位</div><div>${(META.jobs||[]).length} 个（含已停用）</div>
    </div>
  </div>`;
  loadFeedback();
  renderMailCfgInto('mailCfgBox');       // 收信（IMAP）
  renderSmtpCfgInto('smtpCfgBox');       // 发信（SMTP）
  renderTplMgrInto('tplMgrBox');         // 邮件模板管理
  renderImportCfgInto('importCfgBox');   // 来源目录 / 文件清单 / 历史导入记录
  renderDictBox();   // 必须在 innerHTML 之后调用

}

/* 口径偏差（决策反馈闭环）：把"系统建议 vs HR 决定"的偏差摊开给 HR 看。
   数据本来就躺在 applications 表里，这里只是把它算清楚、说人话。
   样本不足时如实说不足，不硬编趋势。 */
async function loadFeedback(){
  const box = document.getElementById('fbBox');
  if (!box) return;
  const r = await api('/api/feedback/report?days=90');
  if (r.__http_error || r.error){
    box.innerHTML = '<div class="small">报告加载失败</div>'; return;
  }
  if (r.insufficient){
    box.innerHTML = `<div class="warn-txt small">${esc(r.message)}</div>
      <div class="small" style="margin-top:6px">怎么看数据够不够：在人才库把候选人的档位
      用下拉框确认（变成"已确认"）即可累积样本。</div>`;
    return;
  }
  const T = ['A','B','C','D'];
  const rows = T.map(s => `<tr><td><b>${s}</b></td>${T.map(f => {
    const n = ((r.matrix||{})[s]||{})[f] || 0;
    const diag = s === f;
    const bg = diag ? '#e8ffe8' : (n ? 'var(--surface)1f0' : '');
    return `<td style="${bg?('background:'+bg+';'):''}${diag?'font-weight:600':''}">${n||'—'}</td>`;
  }).join('')}</tr>`).join('');
  const attr = (r.attribution||[]).map(a =>
    `<li>${esc(a.feature)}：被低估组 <b>${a.low}%</b> / 被高估组 ${a.high}% / 全体 ${a.all}%</li>`).join('');
  box.innerHTML = `
    <div class="kv" style="margin-bottom:10px">
      <div class="k">样本</div><div>最近 ${r.days} 天已确认 <b>${r.total}</b> 份</div>
      <div class="k">一致性</div><div>${r.consistency}% —— 一致 ${r.same} ｜
        <span style="color:${r.high?'var(--warn)':'var(--ink-3)'}">系统偏高 ${r.high}</span> ｜
        <span style="color:${r.low?'var(--warn)':'var(--ink-3)'}">系统偏低 ${r.low}</span></div>
    </div>
    <table style="width:auto">
      <thead><tr><th>建议 ↓ / 实际 →</th>${T.map(x=>`<th>${x}</th>`).join('')}</tr></thead>
      <tbody>${rows}</tbody></table>
    <div class="small" style="margin-top:4px">绿色格=建议与决定一致；红色格=有偏差</div>
    ${attr?`<div style="margin-top:12px"><b>偏差归因</b>
      <ul class="small" style="margin:6px 0 0 18px">${attr}</ul></div>`:''}
    <div style="margin-top:12px"><b>建议</b>
      <ul class="small" style="margin:6px 0 0 18px">${
        (r.suggestions||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>
    <div class="bar" style="margin-top:10px">
      <button onclick="window.open('/api/feedback/export?days=90')">导出明细 CSV</button>
      <span class="small">${esc(r.note||'')}</span>
    </div>`;
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
