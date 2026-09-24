"""工作台界面：单页应用（HTML + 原生 JS，无构建步骤、无外部 CDN）。

九个视图：人才库（顶部内嵌投递管道折叠条） / 归档 / 智能助手 / 导入与来源 / 岗位管理 /
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

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="tp-build" content="__UI_BUILD__">
<title>企业人才库智能体</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;background:#f7f8fa;color:#1d2129;
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
  .side{width:220px;flex:0 0 220px;background:#fff;border-right:1px solid #e5e6eb;
    padding:20px 12px 14px;position:sticky;top:0;height:100vh;
    display:flex;flex-direction:column;box-sizing:border-box}
  .brand{display:flex;align-items:center;gap:10px;font-size:15px;font-weight:600;
    padding:4px 10px 18px;white-space:nowrap}
  .logo{width:30px;height:30px;border-radius:8px;background:#3370ff;color:#fff;
    display:flex;align-items:center;justify-content:center;font-size:15px;flex:0 0 auto}
  .nav{flex:1;overflow:auto}
  .navitem{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;
    cursor:pointer;color:#4e5969;font-size:14px;margin-bottom:2px;white-space:nowrap}
  .navitem svg{width:17px;height:17px;flex:0 0 auto}
  .navitem:hover{background:#f2f3f5;color:#1d2129}
  .navitem.on{background:#e8f0ff;color:#1d5fd8;font-weight:500}
  .sidefoot{font-size:12px;color:#86909c;padding:10px 12px 0;border-top:1px solid #f2f3f5}
  .main{flex:1;min-width:0;padding:24px 30px 60px}
  .main-inner{max-width:1240px;margin:0 auto}
  h1{font-size:22px;font-weight:600;margin:0}
  h2{font-size:17px;font-weight:600;margin:0 0 12px}
  .sub{color:#86909c;font-size:13px;margin-top:4px}
  .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .vdiv{width:1px;height:22px;background:#e5e6eb;display:inline-block;flex:none}
  .glbl{font-size:13px;color:#86909c;flex:none}
  input,select,textarea{padding:7px 12px;border:1px solid #e5e6eb;border-radius:6px;
    font-size:14px;color:#1d2129;background:#fff;font-family:inherit}
  button{padding:6px 14px;border:1px solid #e5e6eb;border-radius:6px;background:#fff;
    font-size:14px;color:#1d2129;cursor:pointer;font-family:inherit}
  button:hover{color:#3370ff;border-color:#c0d0ff;background:#f5f8ff}
  button:disabled{opacity:.5;cursor:not-allowed}
  .btn-primary{background:#3370ff;border-color:#3370ff;color:#fff}
  .btn-primary:hover{background:#245bdb;border-color:#245bdb;color:#fff}
  .btn-ok{background:#00b42a;border-color:#00b42a;color:#fff}
  .btn-ok:hover{background:#0a8f24;border-color:#0a8f24;color:#fff}
  .btn-danger{background:#fff;border-color:#fbaca3;color:#cb2634}
  .btn-danger:hover{background:#ffece8;border-color:#f53f3f;color:#cb2634}
  td button{padding:3px 10px;font-size:13px}
  .card{background:#fff;border:1px solid #e5e6eb;border-radius:12px;padding:16px 18px;margin-bottom:12px}
  .panel{background:#fff;border:1px solid #e5e6eb;border-radius:12px;padding:18px 20px;margin-bottom:14px}
  .grid-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px;margin:16px 0}
  .stat{background:#fff;border:1px solid #e5e6eb;border-radius:10px;padding:12px 14px}
  .stat .k{color:#86909c;font-size:13px}
  .stat .v{font-size:26px;font-weight:600;margin-top:2px}
  .tabs{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 12px}
  .tab{padding:7px 15px;border:1px solid #e5e6eb;border-radius:6px;background:#fff;cursor:pointer;font-size:14px;color:#4e5969}
  .tab:hover{color:#3370ff;border-color:#c0d0ff}
  .tab.on{background:#e8f0ff;border-color:#3370ff;color:#1d5fd8;font-weight:500}
  .chip{font-size:13px;padding:2px 9px;border-radius:10px;display:inline-block;margin:0 4px 4px 0}
  .badge{font-size:13px;padding:3px 10px;border-radius:6px;font-weight:500}
  .row1{display:flex;align-items:center;gap:14px}
  .avatar{width:44px;height:44px;border-radius:50%;display:flex;align-items:center;
    justify-content:center;font-size:17px;font-weight:600;flex:0 0 auto}
  .nm{font-size:17px;font-weight:600}
  .meta{font-size:13px;color:#86909c;margin-top:3px}
  .acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px}
  .why{font-size:14px;color:#4e5969;margin-top:10px;line-height:1.75}
  .ev{font-size:13px;color:#86909c;background:#f7f8fa;border-left:2px solid #c9cdd4;
    padding:5px 9px;margin:4px 0;border-radius:0 4px 4px 0}
  .modal{position:fixed;inset:0;background:rgba(29,33,41,.45);display:none;align-items:center;
    justify-content:center;padding:22px;z-index:20}
  .modal.on{display:flex}
  .sheet{background:#fff;border-radius:12px;max-width:920px;width:100%;max-height:88vh;
    overflow:auto;padding:22px 24px}
  pre{white-space:pre-wrap;word-break:break-word;font-size:14px;color:#4e5969;line-height:1.8;
    background:#f7f8fa;padding:14px;border-radius:8px;margin:0}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #f2f3f5;vertical-align:top}
  th{color:#86909c;font-weight:500;background:#fafbfc}
  .note{color:#86909c;font-size:13px;line-height:1.75}
  .warn{background:#fff7e8;border:1px solid #ffe4ba;color:#a45a00;border-radius:8px;padding:10px 13px;font-size:14px}
  .info{background:#e8f0ff;border:1px solid #d3e0ff;color:#1d5fd8;border-radius:8px;padding:10px 13px;font-size:14px}
  .danger{background:#ffece8;border:1px solid #ffd2c8;color:#cb2634;border-radius:8px;padding:10px 13px;font-size:14px}
  .ok{background:#e8ffea;border:1px solid #c9f2cd;color:#0a7f1f;border-radius:8px;padding:10px 13px;font-size:14px}
  .chatlog{margin-top:12px;max-height:440px;overflow:auto;display:flex;flex-direction:column;gap:9px}
  .msg{font-size:15px;line-height:1.75;padding:10px 13px;border-radius:9px;white-space:pre-wrap}
  .msg.user{background:#e8f0ff;align-self:flex-end;max-width:78%}
  .msg.assistant{background:#f7f8fa;max-width:94%}
  .trace{font-size:13px;color:#86909c;margin-top:8px;border-top:1px dashed #e5e6eb;padding-top:8px}
  .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
  .pcol{background:#fff;border:1px solid #e5e6eb;border-radius:10px;padding:12px 14px}
  .pcol .h{display:flex;justify-content:space-between;font-size:14px;color:#4e5969;font-weight:600}
  .pcol .it{font-size:13px;color:#86909c;margin-top:6px;border-top:1px dashed #f2f3f5;padding-top:6px}
  .flexbetween{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
  .small{font-size:12px;color:#86909c}
  .kv{display:grid;grid-template-columns:140px 1fr;gap:6px 12px;font-size:14px}
  .kv .k{color:#86909c}
  .spacer{height:10px}
  .job-tag{color:#1d5fd8;background:#e8f0ff}
  .job-tag.pending{color:#a45a00;background:#fff7e8}
  .contact{font-size:14px}
  .contact a{color:#1d5fd8;text-decoration:none;border-bottom:1px dashed #b8ccff}
  .contact a:hover{color:#0e42d2;border-bottom-style:solid}
  button.mini{font-size:12px;padding:1px 7px;margin-left:6px;border-radius:6px}
  .srcbox{background:#f7f8fa;border:1px solid #e5e6eb;border-radius:8px;padding:10px 12px;font-size:14px}
  .srcbox code{background:#eef1f6;padding:1px 6px;border-radius:5px;font-size:13px}
  .jdform{display:grid;grid-template-columns:150px 1fr;gap:8px 12px;align-items:center;margin-top:6px}
  .jdform label{color:#4e5969;font-size:14px}
  .jdform input,.jdform textarea,.jdform select{width:100%}
  /* 岗位表的 JD 摘要可能很长，限宽后把「投递数/状态/操作」挤窄了会换行，
     所以 JD 列限宽 + 其余列禁止折行 */
  .jdsum{max-width:430px;line-height:1.6}
  .nw{white-space:nowrap}
  .res-cell{font-size:13px;line-height:1.5}
  .ok-txt{color:#0a7f1f}.warn-txt{color:#a45a00}.bad-txt{color:#f53f3f}
</style></head>
<body>
<div class="layout">
  <aside class="side">
    <div class="brand"><span class="logo">才</span><span>企业人才库智能体</span></div>
    <nav class="nav" id="tabs"></nav>
    <div class="sidefoot" id="sideUser"></div>
  </aside>
  <main class="main"><div class="main-inner">
    <div class="grid-stats" id="stats"></div>
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
const TIER_COLOR = {A:['#00b42a','#e8ffea'],B:['#3370ff','#e8f0ff'],
                    C:['#ff7d00','#fff7e8'],D:['#86909c','#f2f3f5']};
const STAGES = ['新投递','已联系','初面','复面','待offer','已入职','已结束'];

let META = null;
let TOKEN = localStorage.getItem('tp_token') || '';
let VIEW = 'pool', TAB = 'ALL', KW = '', CHAT = [], ITEMS = [], CUR = null, LAST_INGEST = null;
// 性别筛选：**默认不筛**（空串）。开关在设置里默认关闭，关着时后端也会忽略这个参数。
let GENDER = '';
// 人才库分页（v1.7.1）：每页 10 人。切档位 / 搜索 / 清空都会把页码拨回第 1 页——
// 否则"在第 3 页改了搜索词"会落在一个不存在的页上（后端会兜底夹到末页，但那不是用户想要的）。
// 导出 CSV 不分页：另发一次不带 page 参数的请求拿全量，见 exportCsv。
let POOL_PAGE = 1;const POOL_SIZE = 10;
// 投递管道折叠条（嵌入人才库）：记录展开/收起状态，翻页/刷新后保持原状
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
  if (!p && !m) return '<span class="small" style="color:#86909c">未识别到联系方式</span>';
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

const VIEWS = [['pool','人才库'],['archive','归档'],['chat','智能助手'],
               ['import','导入与来源'],['org','岗位管理'],['props','提案与审计'],
               ['search','检索'],['mailcfg','邮箱配置'],['sys','系统说明']];
// 侧栏导航图标：内联 SVG（stroke 跟随文字色），不引外部图标库
const _I = p => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
  stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
const NAV_ICONS = {
  pool:    _I('<path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><circle cx="10" cy="7" r="4"/><path d="M21 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'),
  archive: _I('<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><path d="M10 12h4"/>'),
  chat:    _I('<path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2z"/>'),
  import:  _I('<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>'),
  org:     _I('<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M9 8h.01M15 8h.01M9 12h.01M15 12h.01M9 16h.01M15 16h.01"/>'),
  props:   _I('<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>'),
  search:  _I('<circle cx="11" cy="11" r="7"/><path d="m21 21-4.35-4.35"/>'),
  mailcfg: _I('<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>'),
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
  const st = await api('/api/stats');
  renderStats(st);
  if (VIEW==='pool') await viewPool();
  else if (VIEW==='archive') await viewArchive();
  else if (VIEW==='chat') await viewChat();
  else if (VIEW==='import') await viewImport();
  else if (VIEW==='org') await viewOrg();
  else if (VIEW==='props') await viewProps();
  else if (VIEW==='search') await viewSearch();
  else if (VIEW==='mailcfg') await viewMailCfg();
  else if (VIEW==='sys') await viewSys();
}

function renderStats(s){
  if (!s || s.__http_error) return;
  const t = s.tiers || {};
  document.getElementById('stats').innerHTML =
    card('候选人数', s.people, '#1d2129') + card('A 优先面试', t.A||0, '#00b42a') +
    card('B 建议面试', t.B||0, '#3370ff') + card('C 储备', t.C||0, '#ff7d00') +
    card('D 暂不匹配', t.D||0, '#86909c') +
    card('待 HR 确认', s.pending, '#f53f3f') + card('待人工判读', s.needs_review, '#ff7d00') +
    card('投递数', s.applications, '#1d2129') + card('简历附件', s.documents, '#1d2129') +
    card('待确认提案', s.proposals_pending, '#f53f3f');
}
function card(k,v,c){ return `<div class="stat"><div class="k">${k}</div><div class="v" style="color:${c}">${v==null?'—':v}</div></div>`; }

/* ------------------------------ 人才库 ------------------------------ */
async function viewPool(){
  // 管道条与人选列表各取一份：/api/pipeline 提供各阶段人数与明细（嵌入本页顶部），
  // /api/candidates 提供当前筛选 + 分页的候选人卡片，两者互不影响。
  const [c, p] = await Promise.all([
    api('/api/candidates?tier=' + encodeURIComponent(TAB)
      + '&kw=' + encodeURIComponent(KW) + '&gender=' + encodeURIComponent(GENDER)
      + '&page=' + POOL_PAGE + '&page_size=' + POOL_SIZE),
    api('/api/pipeline')
  ]);
  ITEMS = c.items || [];
  const pg = c.paging || null;              // 后端算好的页码/总数（页大小不影响统计口径）
  const gf = c.gender_filter || {}, gfOn = !!gf.enabled;
  const host = document.getElementById('view');
  const n = {ALL:(pg?pg.total:(c.items||[]).length), REVIEW:0, UNCONFIRMED:0};
  const tabs = [['ALL','全部'],['A','A 优先面试'],['B','B 建议面试'],['C','C 储备'],
                ['D','D 暂不匹配'],['REVIEW','待人工判读'],['UNCONFIRMED','待 HR 确认']];
  // 性别筛选只在开关打开时出现：开关关着还摆一个筛选项，等于诱导所有人按性别筛。
  const facets = c.gender_facets || {};
  const gsel = !gfOn ? '' : `<span class="small" style="margin-left:6px">性别</span>
    <select onchange="GENDER=this.value;poolPageReset();refresh()">
      <option value="">不限</option>
      ${['男','女','未标注'].map(g=>`<option value="${g}" ${GENDER===g?'selected':''}>${g}（${facets[g]==null?0:facets[g]}）</option>`).join('')}
    </select>`;
  const gtip = (!gfOn && gf.requested)
    ? `<div class="warn" style="margin-top:8px">性别筛选未生效：${esc(gf.why||'')}</div>` : '';
  host.innerHTML = `
  <div class="panel">
    <div class="flexbetween">
      <div class="bar">
        <input id="kwBox" placeholder="搜索姓名 / 院校 / 专业 / 技能" style="width:280px" value="${esc(KW)}"
               onkeydown="if(event.key==='Enter'){KW=this.value;poolPageReset();refresh()}">
        <button class="btn-primary" onclick="KW=document.getElementById('kwBox').value;poolPageReset();refresh()">搜索</button>
        <button onclick="KW='';GENDER='';poolPageReset();refresh()">清空</button>
        ${gsel}
      </div>
      <div class="bar">
        <button class="btn-primary" onclick="go('import')">收简历 / 看来源</button>
        <button onclick="doIngest('mailbox')">收取邮箱简历</button>
        <button onclick="doIngest('folder')">导入本地文件夹</button>
        <button onclick="exportCsv()">导出 CSV</button>
      </div>
    </div>
    <div class="bar" style="margin-top:8px">
      <span class="small">批量归档（按年使用：新一年开始把旧简历收起来）</span>
      <button onclick="archiveBatch(null,true)">归档勾选的人</button>
      <input id="poolArchYear" type="number" placeholder="年份，如 2025" style="width:130px">
      <button onclick="archiveByYearFrom('poolArchYear')">归档该年以前</button>
      <span class="small">归档的人进「归档」页，满 30 天自动彻底删除，期间可随时取消</span>
    </div>
    ${gtip}
    <div class="tabs" style="margin-bottom:0">
      ${tabs.map(([k,l])=>`<div class="tab ${TAB===k?'on':''}" onclick="TAB='${k}';poolPageReset();refresh()">${l}${k==='ALL'?(' '+n.ALL):''}</div>`).join('')}
    </div>
  </div>
  ${pipeCardHtml(p)}
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
    <span class="small" style="color:#86909c">导出 CSV 不受分页影响，始终导出当前筛选的全部人</span>
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
function cardHtml(x){
  const t = x.tier_effective || 'D';
  const [fg,bg] = TIER_COLOR[t] || TIER_COLOR.D;
  const job = x.job_title || null;
  const sug = x.job_suggestion || null;
  // 建议岗位（v1.5）：入库时就把**每个在招岗位**的 JD 试算了一遍，取最匹配的那个，
  // 并且卡片上的评分/档位就是**按这个岗位的尺子**算的——所以这里没有"材料类默认尺子"
  // 造成的错标（一位 Java 工程师不会再被钛合金尺子打成 D 档）。
  const sugChip = (!job && sug)
    ? `<span class="chip" style="color:#1d5fd8;background:#e8f0ff"
         title="入库时对全部在招岗位逐个试算（JD 尺子），取分数最高者；采纳后才真正归岗${sug.source==='live'?'（本条为老数据，展示时现算）':''}">建议岗位：${esc(sug.title)}（匹配 ${sug.score==null?'—':sug.score} · ${esc(sug.tier_suggested)} 档）</span>`
    : (job ? '' : ((x.job_suggestions_considered||0) > 0
        ? `<span class="chip" style="color:#86909c;background:#f2f3f5"
             title="已对 ${x.job_suggestions_considered} 个在招岗位逐个试算，均无技能交集">与所有在招岗位均无交集，保持待指定</span>`
        : `<span class="chip" style="color:#86909c;background:#f2f3f5">暂无在招岗位可试算，暂按默认尺子打分</span>`));
  const jobTag = job
    ? `<span class="chip job-tag">${esc(job)}</span>`
    : `<span class="chip job-tag pending">所属岗位待指定</span>${sugChip}`;
  // 性别标签：只在简历**明写**时才有值（系统不做推断），提示里说明它不参与评分
  const genderTag = (x.gender||'').trim()
    ? `<span class="chip" style="color:#4e5969;background:#f2f3f5" title="来自简历明写标签，不参与评分与分级">${esc(x.gender)}</span>`
    : '';
  const hits = (x.hits||[]).map(s=>`<span class="chip" style="color:#00b42a;background:#e8ffea">命中 ${esc(s)}</span>`).join('');
  const miss = (x.miss||[]).map(s=>`<span class="chip" style="color:#ff7d00;background:#fff7e8">缺 ${esc(s)}</span>`).join('');
  const conf = (x.app_status==='已确认')
    ? '<span class="chip" style="color:#0a7f1f;background:#e8ffea">HR 已确认</span>'
    : '<span class="chip" style="color:#f53f3f;background:#ffece8">待确认</span>';
  const rev = x.needs_review ? '<span class="chip" style="color:#a45a00;background:#fff7e8">待人工判读</span>' : '';
  const stage = x.stage || '新投递';
  const tiers = ['A','B','C','D'].map(k=>`<option value="${k}" ${k===t?'selected':''}>${k} · ${TIER_LABELS[k]}</option>`).join('');
  const stages = STAGES.map(k=>`<option value="${k}" ${k===stage?'selected':''}>${k}</option>`).join('');
  const skillChips = (x.skills||[]).slice(0,12).map(s=>`<span class="chip" style="color:#3370ff;background:#e8f0ff">${esc(s)}</span>`).join('');
  const contact = contactLine(x);
  const scoreTip = (!job && sug)
    ? ` title="评分与档位按「建议岗位 · ${esc(sug.title)}」的 JD 尺子试算（不是材料类默认尺子）"` : '';
  return `<div class="card">
    <div class="row1">
      <input type="checkbox" class="pickChk" value="${x.id}" style="margin-right:10px">
      <div class="avatar" style="color:${fg};background:${bg}">${esc((x.name||'?').slice(0,1))}</div>
      <div style="flex:1;min-width:0">
        <div class="nm">${esc(x.name||'未识别')} ${rev} ${genderTag} ${jobTag}</div>
        <div class="meta">${esc(x.edu_level||'—')} · ${x.years_exp==null?'—':x.years_exp+' 年'} ·
          ${esc(x.school||'—')} · 匹配 ${(x.score==null?'—':x.score)} · 阶段 ${esc(stage)} ·
          来源 ${esc(x.channel||'—')} · 投递 ${esc((x.applied_at||'').slice(0,10))}</div>
        <div class="meta" style="margin-top:2px"><b>联系方式</b>：${contact}</div>
      </div>
      <div class="badge"${scoreTip} style="color:${fg};background:${bg}">${t} · ${TIER_LABELS[t]}</div>
    </div>
    <div style="margin-top:10px">${hits}${miss}${conf}</div>
    <div style="margin-top:8px">${skillChips}</div>
    <div class="evs">${(x.hit_detail||[]).slice(0,3).map(h=>
      `<div class="ev">证据[${esc(h.skill)}]：${esc(h.evidence)}</div>`).join('')}</div>
    <div class="why">推荐理由：${esc((x.reasons||[]).join('；')||'—')}</div>
    <div class="acts">
      <select onchange="setTier(${x.application_id},this.value)">${tiers}</select>
      <select onchange="setStage(${x.application_id},this.value)">${stages}</select>
      <button onclick="showDetail(${x.id})">完整档案</button>
      <button onclick="explain(${x.id})">档位解释</button>
      <button onclick="analyze(${x.id})">模型分析</button>
      <button onclick="interview(${x.id})">面试提纲</button>
      ${sug ? `<span class="vdiv"></span>
      <button class="btn-primary" onclick="assignJob(${x.id},${sug.job_id},'${esc(sug.title)}')">采纳建议岗位</button>` : ''}
      <span class="vdiv"></span>
      <button class="btn-danger" onclick="archiveCandidate(${x.id},true)">归档</button>
    </div>
    <div id="out-${x.id}"></div>
  </div>`;
}
async function setTier(aid, tier){
  if (!aid) { toast('该候选人暂无投递记录', 'warn'); return; }
  const r = await api('/api/applications/'+aid+'/tier', {method:'POST', body:JSON.stringify({tier:tier})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'改档失败','danger'); return; }
  toast('已确认档位：'+tier+'（已写入审计）','ok'); refresh();
}
async function setStage(aid, stage){
  if (!aid) return;
  const r = await api('/api/applications/'+aid+'/stage', {method:'POST', body:JSON.stringify({stage:stage})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'推进失败','danger'); return; }
  toast('阶段已推进到「'+stage+'」','ok'); refresh();
}
async function showDetail(cid){
  const d = await api('/api/candidates/'+cid);
  if (d.__http_error){ toast(d.detail||'读取失败','danger'); return; }
  document.getElementById('mTitle').textContent = (d.name||'未识别') + ' · 完整档案';
  const skills = (d.skills||[]).map(s=>`<span class="chip" style="color:${s.verified?'#3370ff':'#86909c'};
      background:${s.verified?'#e8f0ff':'#f2f3f5'}">${esc(s.name)}${s.verified?'':'(未核验)'}</span>`).join('');
  const evs = (d.skills||[]).filter(s=>s.evidence).slice(0,10).map(s=>
      `<div class="ev">${esc(s.name)}：${esc(s.evidence)}</div>`).join('');
  const apps = (d.applications||[]).map(a=>`<tr>
      <td>#${a.id}</td><td>${esc(a.job_title||'待指定')}</td><td>${esc(a.channel||'—')}</td>
      <td>${esc((a.applied_at||'').slice(0,16))}</td>
      <td>${esc(a.tier_final||a.tier_suggested||'—')}</td><td>${esc(a.stage||'—')}</td>
      <td>${esc(a.status||'—')}</td><td>${a.score==null?'—':a.score}</td></tr>`).join('');
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
      <div class="k">学历 / 年限</div><div>${esc(d.edu_level||'—')} · ${d.years_exp==null?'—':d.years_exp+' 年'}</div>
      <div class="k">院校 / 专业</div><div>${esc(d.school||'—')} · ${esc(d.major||'—')}</div>
      <div class="k">性别</div><div>${(d.gender||'').trim()
        ? esc(d.gender) + ' <span class="small">（简历明写；不参与评分与分级）</span>'
        : '<span class="small">简历未写性别（系统不做推断）</span>'}</div>
      <div class="k">联系方式</div><div>${(()=>{
        const p=contactValue(d.phone), m=contactValue(d.email);
        if(!p && !m) return '<span class="small">未识别到联系方式（简历里可能确实没写）</span>';
        return `<span class="contact">${p?`<a href="tel:${esc(p)}">${esc(p)}</a>`:''}
          ${p&&m?' · ':''}${m?`<a href="mailto:${esc(m)}">${esc(m)}</a>`:''}</span>
          <button class="mini" onclick="copyText('${esc((p||'')+(p&&m?' / ':'')+(m||''))}')">复制</button>
          <span class="small">（库内加密存储）</span>`;
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
      <th>阶段</th><th>状态</th><th>评分</th></tr></thead><tbody>${apps||'<tr><td colspan="8">—</td></tr>'}</tbody></table>
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
  const color = mm.verdict==='错配' ? '#f53f3f' : (mm.verdict==='对口' ? '#00b42a' : '#ff7d00');
  const list = c => Object.entries(c||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>k+'（'+v+'）').join('、')||'—';
  const conf = mm.confidence || '';
  const confColor = conf==='高' ? '#00b42a' : (conf==='中' ? '#ff7d00' : '#f53f3f');
  const chName = {大类:'按技能大类', 专业:'按专业维度', 词面:'按文字比对', 无:'无可用依据'}[mm.channel] || mm.channel || '';
  const mc = mm.major_check || {};
  const inList = mc.in_list === true ? '<b style="color:#00b42a">在清单内</b>'
               : mc.in_list === false ? '<b style="color:#f53f3f">不在清单内</b>'
               : '<b style="color:#86909c">未识别</b>';
  const un = mm.unclassified || {};
  return `<div style="margin-top:8px;padding-top:8px;border-top:1px dashed #ddd">
    <b style="color:${color}">专业方向匹配：${esc(mm.verdict)}</b>`
    + (conf?`<span class="chip" style="margin-left:6px;color:${confColor};background:${conf==='高'?'#e8ffea':(conf==='中'?'#fff7e8':'#ffece8')}">置信度 ${esc(conf)}</span>`:'')
    + (chName?`<span class="small">（${esc(chName)}）</span>`:'')
    + (mm.major_label?`<span class="small">｜专业：${esc(mm.major_label)}</span>`:'')
    + `<br><span class="small">岗位侧重 ${esc(list(mm.job_categories))}｜候选人技能 ${esc(list(mm.cand_categories))}</span>`
    + ((un.job||un.cand)?`<br><span class="small" style="color:#86909c">本体未收录：岗位侧 ${un.job||0} 项、候选人侧 ${un.cand||0} 项——它不计入「侧重」统计，但已进入文字比对通道</span>`:'')
    + ((mc.required||[]).length?`<br><span class="small">专业需求：${esc((mc.required||[]).join('、'))} → 候选人专业 ${inList}</span>`:'')
    + `<br><span class="small">${esc(mm.note)}</span></div>`;
}
async function explain(cid){
  const el = document.getElementById('out-'+cid);
  el.innerHTML = '<div class="note">计算中…</div>';
  const r = await api('/api/candidates/'+cid+'/explain', {method:'POST'});
  if (r.error){ el.innerHTML = '<div class="warn">'+esc(r.error)+(r.hint?('<br>'+esc(r.hint)):'')+'</div>'; return; }
  el.innerHTML = `<div class="why" style="background:#f7f8fa;padding:12px;border-radius:8px;margin-top:10px">
    ${jobLine(r.job,'档位解释')}
    <b>档位解释（纯规则，可复现）→ 建议 ${esc(r.tier_suggested)}，评分 ${r.score}</b><br>
    分值拆解：${Object.entries(r.breakdown||{}).map(([k,v])=>k+' '+v).join(' / ')}<br>
    命中：${(r.hit||[]).map(x=>esc(x.skill)).join('、')||'—'}；缺失：${(r.miss||[]).join('、')||'—'}<br>
    ${(r.miss_custom||[]).length?`<span class="small">其中「岗位自定义要求」（本体未收录、按文字比对）未命中：${esc((r.miss_custom||[]).join('、'))}<br></span>`:''}
    ${(r.hit||[]).map(x=>`<div class="ev">${esc(x.skill)}：${esc(x.evidence)}</div>`).join('')}
    ${majorBlock(r.major_match)}
    ${r.consistency && !r.consistency.same ? `<div class="small" style="color:#ff7d00;margin-top:6px">
      <b>注意：库内记录与当前重算不一致</b>（库内 ${esc(r.consistency.db_tier)} / ${r.consistency.db_score}
      ↔ 当前 ${esc(r.tier_suggested)} / ${r.score}）。${esc(r.consistency.note)}</div>` : ''}
    ${(r.risks||[]).length?('<div class="small">风险提示：'+esc(r.risks.join('；'))+'</div>'):''}
    </div>`;
}
async function analyze(cid){
  const el = document.getElementById('out-'+cid);
  el.innerHTML = '<div class="note">模型分析中…</div>';
  const r = await api('/api/candidates/'+cid+'/analyze', {method:'POST'});
  if (r.error){ el.innerHTML = '<div class="warn">'+esc(r.error)+(r.hint?('<br>'+esc(r.hint)):'')+'</div>'; return; }
  el.innerHTML = `<div class="why" style="background:#f7f8fa;padding:12px;border-radius:8px;margin-top:10px">
    ${jobLine(r.job,'模型分析')}
    <b>模型判断：建议 ${esc(r.suggested_tier||'-')}（置信度 ${r.confidence==null?'-':r.confidence}）</b><br>
    ${esc(r.summary||'')}<br>亮点：${esc((r.highlights||[]).join('；')||'—')}<br>
    风险：${esc((r.risks||[]).join('；')||'—')}<br>
    <span class="small">模型建议仅供参照，最终档位由 HR 确认。</span></div>`;
}
async function interview(cid){
  const el = document.getElementById('out-'+cid);
  el.innerHTML = '<div class="note">生成面试提纲中…</div>';
  const r = await api('/api/candidates/'+cid+'/interview', {method:'POST', body:JSON.stringify({focus:''})});
  if (r.error){ el.innerHTML = '<div class="warn">'+esc(r.error)+(r.hint?('<br>'+esc(r.hint)):'')+'</div>'; return; }
  el.innerHTML = `<div class="why" style="background:#f7f8fa;padding:12px;border-radius:8px;margin-top:10px">
    ${jobLine(r.job,'面试提纲')}
    <b>面试提纲（模型生成，供参考）</b>${(r.questions||[]).map((q,i)=>
      `<div style="margin-top:6px"><b>${i+1}. ${esc(q.q)}</b>
       <div class="small">考察：${esc(q.why||'')}</div></div>`).join('')}
    <div class="small" style="margin-top:8px">提纲按「该候选人对应岗位」的必需技能与职责生成；
      未归岗的投递建议先归岗或采纳建议岗位，否则题目会缺少针对性。</div></div>`;
}
async function doIngest(source){
  const label = source==='mailbox' ? '邮箱' : '本地文件夹';
  toast('正在收取简历…（'+label+'）','info');
  const r = await api('/api/ingest', {method:'POST', body:JSON.stringify({source:source})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'收取失败','danger'); return; }
  const num = k => (r[k]==null?0:r[k]);
  const ix = r.index || {};
  toast(`完成：新增 ${num('added')}，新版本 ${num('merged_versions')}，`
    + `跳过重复 ${num('skipped_dup')}，无附件 ${num('no_attachment')}，`
    + `解析失败(仍入库) ${num('parse_failed')}，超限跳过 ${num('skipped_oversize')}，`
    + `异常 ${num('failed')}`
    + `｜源：${r.source_label||label}`
    + `｜索引：新增 ${ix.indexed==null?0:ix.indexed} 条（跳过 ${ix.skipped==null?0:ix.skipped} 条）`,
    (r.failed || r.skipped_oversize) ? 'warn':'ok');
  LAST_INGEST = r;                       // 明细留在「导入与来源」页看
  await refresh();
}
async function exportCsv(){
  // 导出内容按试用反馈定：**个人简介 + 对应岗位**，不掺内部评分口径。
  // 这份 CSV 是拿去用的（发给用人部门、贴进汇报、做面试排期），
  // 档位/命中/推荐理由属于系统内部判断，HR 要看在界面里看即可。
  // v1.7.1 起人才库分页展示——**导出必须拿全量**，不能只导当前页：
  // 另发一次不带 page 参数的请求（接口默认返回全量，兼容口径保留着），
  // 筛选条件（档位/关键词/性别）与当前列表保持一致。
  const full = await api('/api/candidates?tier=' + encodeURIComponent(TAB)
    + '&kw=' + encodeURIComponent(KW) + '&gender=' + encodeURIComponent(GENDER));
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

/* --------------------- 投递管道（嵌入人才库，v1.7.2） ---------------------
   原独立「投递管道」导航页整体退出（VIEWS 已移除，#pipe 旧链接自动落回人才库），
   改为人才库列表上方的一条可折叠管道条：
   · 收起（默认）：只看各阶段人数，超期红字提醒；
   · 展开：按阶段列出人，点姓名直接打开该人的完整档案。
   数据仍是 /api/pipeline 一份，不新开接口；展开状态记在 PIPE_OPEN，
   翻页 / 搜索 / 改档位等 refresh 重渲染后不丢。 */
function pipeToggle(){
  PIPE_OPEN = !PIPE_OPEN;
  const b = document.getElementById('pipeBody');
  if (b) b.style.display = PIPE_OPEN ? '' : 'none';
  const a = document.getElementById('pipeArrow');
  if (a) a.textContent = PIPE_OPEN ? '收起 ▲' : '展开 ▼';
}
function pipeCardHtml(p){
  const order = (p && p.stage_order) || STAGES;
  const st = (p && p.stages) || {};
  const openTotal = (p && p.open_total) || 0;
  // 收起态的一行阶段人数：没人的阶段不摆出来，有超期的红字
  const chips = order.map(s=>{
    const v = st[s] || {count:0, overdue:0};
    if (!v.count) return '';
    return `<span class="chip" style="color:${v.overdue?'#f53f3f':'#1d5fd8'};background:${v.overdue?'#ffece8':'#e8f0ff'}"
      title="${esc(s)}：${v.count} 人，点击展开看人">${esc(s)} ${v.count}${v.overdue?('（超期 '+v.overdue+'）'):''}</span>`;
  }).join('');
  // 展开态：按阶段列出人（后端每阶段最多回 20 条，多了给条提示），点姓名开完整档案
  const body = order.map(s=>{
    const v = st[s] || {count:0, items:[], overdue:0};
    if (!v.count) return '';
    const rows = (v.items||[]).map(i=>`
      <div style="padding:3px 0;cursor:pointer;color:#1d5fd8" title="点击打开完整档案"
           onclick="showDetail(${i.candidate_id})">
        ${esc(i.candidate_name||'未识别')}${i.job_title?(' · '+esc(i.job_title)):''}
        <span class="small" style="color:#86909c">${esc((i.applied_at||'').slice(0,10))} 起 ${i.days} 天
        ${i.days>=15?'<span style="color:#f53f3f">超期</span>':''}</span>
      </div>`).join('');
    const more = v.count > (v.items||[]).length
      ? `<div class="small" style="color:#86909c">该阶段共 ${v.count} 人，此处最多列 20 人（完整名单见对应阶段筛选）</div>` : '';
    return `<div style="margin-top:8px"><b>${esc(s)}</b>
      <span class="small" style="color:${v.overdue?'#f53f3f':'#86909c'}">${v.count} 人${v.overdue?(' · 超期 '+v.overdue):''}</span>
      <div style="margin-top:2px">${rows}${more}</div></div>`;
  }).join('') || '<div class="small" style="color:#86909c">暂无在流程中的投递。</div>';
  return `<div class="card" style="margin-bottom:12px">
    <div style="display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none" onclick="pipeToggle()">
      <b>投递管道</b>
      <span class="small" style="color:#4e5969">在流程中 ${openTotal} 条（已入职 / 已结束不计）</span>
      <span style="flex:1"></span>
      <span id="pipeArrow" class="small" style="color:#1d5fd8">${PIPE_OPEN?'收起 ▲':'展开 ▼'}</span>
    </div>
    <div class="bar" style="margin-top:8px;cursor:pointer" onclick="pipeToggle()">
      ${chips || '<span class="small" style="color:#86909c">各阶段暂无人</span>'}
    </div>
    <div id="pipeBody" style="display:${PIPE_OPEN?'':'none'}">${body}</div>
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
      : (expired ? `<span class="chip" style="color:#f53f3f;background:#ffece8">已满 30 天，可彻底删除</span>`
                 : `<span class="chip" style="color:${expiring?'#f53f3f':'#4e5969'};background:${expiring?'#ffece8':'#f2f3f5'}"
                      title="到期后自动彻底删除，原件移入回收目录">还有 ${left} 天彻底删除（${esc(am.purge_at||'')}）</span>`);
    return `<div class="card">
      <div class="row1">
        <input type="checkbox" class="archChk" value="${x.id}" style="margin-right:10px">
        <div class="avatar" style="color:${fg};background:${bg}">${esc((x.name||'?').slice(0,1))}</div>
        <div style="flex:1;min-width:0">
          <div class="nm">${esc(x.name||'未识别')} ${leftTxt}</div>
          <div class="meta">${esc(x.edu_level||'—')} · ${x.years_exp==null?'—':x.years_exp+' 年'} ·
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
  if (!confirm(`将${verb} ${list.length} 人。\\n\\n` + (archived
      ? '归档后在「归档」页可见，满 30 天会被彻底删除（期间可随时取消归档）。继续？'
      : '取消归档后立即恢复在人才库与检索中展示。继续？'))) return;
  const r = await api('/api/candidates/archive-batch', {method:'POST',
    body:JSON.stringify({ids:list, archived:archived})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||verb+'失败','danger'); return; }
  toast(`已${verb} ${r.changed} 人${r.skipped?`（跳过 ${r.skipped} 人）`:''}`,'ok');
  refresh();
}
async function archiveByYear(elId){
  const el = document.getElementById(elId || 'archYear') || document.getElementById('poolArchYear');
  const y = parseInt((el||{}).value || '0');
  if (!y){ toast('请先填年份，例如 2026 表示归档 2026 年以前的投递','warn'); return; }
  if (!confirm(`把「最后一条投递早于 ${y} 年」的档案整批归档（当前还在人才库里的）。\\n\\n`
      + `归档后在「归档」页可见，满 30 天会被彻底删除。继续？`)) return;
  const r = await api('/api/candidates/archive-batch', {method:'POST',
    body:JSON.stringify({before_year:y, archived:true})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'批量归档失败','danger'); return; }
  toast(`已归档 ${r.changed} 人（按 ${y} 年以前筛选命中 ${r.picked_by_year} 人）`,'ok');
  refresh();
}
function archiveByYearFrom(elId){ return archiveByYear(elId); }
async function purgeOne(cid){
  if (!confirm('彻底删除后**不可恢复**：档案、投递、附件记录都会删除，'
    + '原件会移入回收目录（需要时请先在磁盘上复制一份）。\\n\\n确定彻底删除？')) return;
  const r = await api('/api/candidates/'+cid+'/purge', {method:'POST'});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'彻底删除失败','danger'); return; }
  toast('已彻底删除（原件移入回收目录，审计已留痕）','ok');
  refresh();
}
async function archiveCandidate(cid, on){
  if (on && !confirm('归档后该候选人将从人才库与检索中隐藏，档案、投递、附件全部保留，'
    + '可在「归档」页随时恢复。\\n\\n继续归档？')) return;
  const r = await api('/api/candidates/'+cid+(on?'/archive':'/unarchive'), {method:'POST'});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'操作失败','danger'); return; }
  toast(on ? '已归档（可在「归档」页查看与恢复）' : '已取消归档，恢复在人才库与检索中展示', 'ok');
  refresh();
}
// 采纳建议岗位：把待指定投递归到 HR 确认的岗位，并按该岗位 JD 重算建议档位
async function assignJob(cid, jobId, title){
  if (!confirm('把该候选人的待指定投递归到「'+title+'」？\\n\\n'
    + '归岗后会按该岗位的 JD 重算建议档位，动作写入审计。')) return;
  const r = await api('/api/candidates/'+cid+'/assign-job',
    {method:'POST', body:JSON.stringify({job_id:jobId})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'归岗失败','danger'); return; }
  toast('已归岗到「'+title+'」'+(r.note?('（'+r.note+'）'):'（已按岗位 JD 重算）'), 'ok');
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
  if (!confirm('清空对话记录？\\n\\n界面上的历史会全部清空，此后从零开始。\\n'
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
  if (!confirm('恢复刚清空的对话？\\n\\n界面上的历史会重新出现。\\n'
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
  if ((r.pending_proposals||[]).length) toast('智能体提交了待确认提案，请到「提案与审计」确认','warn');
}

/* ------------------------------ 导入与来源 ------------------------------ */
// 回答三个问题：从哪儿导？导入了什么？结果怎么样？
async function viewImport(){
  const s = await api('/api/sources');
  const f = s.folder || {}, mbx = s.mailbox || {}, last = LAST_INGEST || s.last_ingest || null;
  const rec = s.recycle || {};
  const files = f.files || [];
  const lim = f.max_attachment_mb==null?20:f.max_attachment_mb;
  const fileRows = files.map(x=>`<tr>
      <td><input type="checkbox" class="fileChk" data-name="${esc(x.name)}"
           data-doc="${x.document_id||''}"></td>
      <td>${esc(x.name)}${x.over_limit
          ? `<span class="chip" style="color:#f53f3f;background:#ffece8">超 ${lim}MB，导入会跳过</span>`:''}</td>
      <td>${fmtSize(x.size)}</td>
      <td>${esc(x.mtime||'')}</td>
      <td>${x.indexed?'<span class="ok-txt">已入库</span>'+(x.candidate?('（'+esc(x.candidate)+'）'):'')
                    :'<span class="warn-txt">尚未入库</span>'}</td>
      <td class="nw">
        <button onclick="openSourceFile('${esc(x.name)}',1)">在线预览</button>
        <button onclick="downloadSourceFile('${esc(x.name)}')">下载</button>
        <button class="btn-danger" onclick="removeSourceFiles(['${esc(x.name)}'])">删除</button>
      </td></tr>`).join('');
  const modeTip = mbx.mode==='imap'
    ? `<b>${esc(mbx.account||'（未配置账号）')}</b>，收件夹 <code>${esc(mbx.folder||'INBOX')}</code>`
      + `，${mbx.readonly?'只读（不删信、不改已读）':'<span class="bad-txt">非只读，请改回只读</span>'}`
      + `，口令 ${mbx.password_set?'<span class="ok-txt">已设置</span>':'<span class="warn-txt">未设置</span>'}`
      + (mbx.cursor?`，已收到游标 <code>${esc(String(mbx.cursor).slice(-24))}</code>`:'')
    : (mbx.mode==='eml'
        ? `演练模式：读本地目录 <code>${esc((s.eml_dir||{}).path||'')}</code>（不连真实邮箱）`
        : '<span class="warn-txt">收信已关闭</span>');
  // 演练模式却填了 IMAP 账号 = "配了没反应"的头号原因，直接在页面上说出来
  const modeWarn = (mbx.mode==='eml' && (mbx.user||mbx.host))
    ? `<div class="warn" style="margin-top:8px">当前是 <b>eml 演练模式</b>：点「收取简历」读的是本地
       .eml 目录，不会连真实邮箱。要真正收信，请把「模式」改为 imap → 点保存。</div>` : '';
  document.getElementById('view').innerHTML = `
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
      <button class="btn-primary" onclick="doIngest('folder')">导入这个文件夹</button>
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

  <div class="card"><h2>② 来源二：邮箱</h2>
    <div class="srcbox">${modeTip}
      <div class="small" style="margin-top:4px">口令只存在本机 <code>config/imap.secret</code>（权限 0600），
        不回显、不写日志。国内邮箱（QQ / 163 / 企业邮）需在邮箱设置里开启 IMAP 并生成
        <b>授权码</b>，口令栏填授权码而不是登录密码。</div></div>
    ${modeWarn}
    <div class="bar" style="margin-top:12px">
      <span class="small">模式</span>
      <select id="ixMode">
        <option value="eml" ${mbx.mode==='eml'?'selected':''}>eml 演练</option>
        <option value="imap" ${mbx.mode==='imap'?'selected':''}>imap 真实收信</option>
        <option value="off" ${mbx.mode==='off'?'selected':''}>off 关闭</option>
      </select>
      <span class="small">服务商</span>
      <select id="ixPreset" onchange="applyMailPreset()"><option value="">（选择后自动填服务器）</option></select>
      <input id="ixHost" value="${esc(mbx.host||'')}" placeholder="imap.exmail.qq.com" style="width:200px">
      <input id="ixPort" value="${mbx.port==null?993:mbx.port}" style="width:80px" title="端口">
      <label style="display:flex;align-items:center;gap:5px" title="企业邮箱一般为 SSL">
        <input type="checkbox" id="ixSsl" ${mbx.ssl===false?'':'checked'}> SSL</label>
      <input id="ixUser" value="${esc(mbx.user||'')}" placeholder="jobs@example.cn" style="width:200px">
      <input id="ixPass" type="password" style="width:160px"
             placeholder="${mbx.password_set?'口令已保存（留空不改）':'授权码 / 口令'}">
      <button onclick="saveInlineMail()">保存</button>
      <button onclick="testInlineMail()">测试连接</button>
    </div>
    <div class="bar" style="margin-top:8px">
      <button class="btn-primary" onclick="previewMail()">先看邮箱里有什么</button>
      <button onclick="doIngest('mailbox')">收取简历</button>
      <button onclick="go('mailcfg')">完整配置（附件白名单、体积上限、归岗窗口等）→</button>
    </div>
    <div id="mailPreview" style="margin-top:10px"></div>
  </div>

  <div class="card"><h2>③ 最近一次导入：导入了什么、结果如何</h2>
    ${last ? ingestDetailHtml(last) : '<div class="note">还没有导入记录。上面两个按钮任一执行一次即可。</div>'}
  </div>`;
  hookFileChecks();
  loadMailPresets();
}
async function loadMailPresets(){
  const sel = document.getElementById('ixPreset');
  if (!sel) return;
  const r = await api('/api/mailbox/presets');
  sel.innerHTML = '<option value="">（选择后自动填服务器）</option>'
    + (r.presets||[]).map(p=>`<option value="${esc(p.host)}|${p.port}|${p.ssl?1:0}">${esc(p.label)}</option>`).join('');
  sel.title = r.note||'';
}
function applyMailPreset(){
  const sel = document.getElementById('ixPreset');
  if (!sel || !sel.value) return;
  const [host, port, ssl] = sel.value.split('|');
  document.getElementById('ixHost').value = host;
  document.getElementById('ixPort').value = port;
  document.getElementById('ixSsl').checked = (ssl === '1');
  toast('已填入 '+host+'（端口 '+port+'）。别忘了把「模式」切到 imap 再点保存。','info');
}
async function removeSelected(){
  const sel = checkedFiles();
  if (!sel.length){ toast('请先勾选要删除的简历','warn'); return; }
  removeSourceFiles(sel.map(x=>x.name));
}
async function removeSourceFiles(names){
  // 二次确认：一次性把一批简历移出来源目录，点错一次要逐个恢复，代价不对称。
  const list = names.slice(0,5).join('、') + (names.length>5 ? (' 等 '+names.length+' 份') : '');
  if (!confirm('将把以下文件移入回收目录（可恢复，不是物理删除）：\\n\\n'+list+'\\n\\n继续？')) return;
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
async function saveInlineMail(){
  // 端口/SSL 一起提交：内联表单以前只改服务器与账号，端口沿用旧值，
  // 换个服务商就会出现"服务器改了、端口还是旧的"这种半生效状态。
  const body = {
    mode: document.getElementById('ixMode').value,
    imap_host: document.getElementById('ixHost').value.trim(),
    imap_port: parseInt(document.getElementById('ixPort').value)||null,
    imap_ssl: document.getElementById('ixSsl').checked,
    imap_user: document.getElementById('ixUser').value.trim(),
  };
  const p = document.getElementById('ixPass').value;
  if (p) body.password = p;
  const r = await api('/api/mailbox/config',{method:'POST',body:JSON.stringify(body)});
  if (r.__http_error||r.error){ toast(r.detail||r.error||'保存失败','danger'); return; }
  const warn = (r.warnings||[]);
  toast('邮箱配置已保存'+(r.password_set?'（口令已更新）':'')+(warn.length?(' ｜ '+warn.join('；')):''),
        warn.length?'warn':'ok');
  META = await api('/api/meta'); refresh();
}
async function testInlineMail(){
  const body = {
    imap_host: document.getElementById('ixHost').value.trim(),
    imap_port: parseInt(document.getElementById('ixPort').value)||null,
    imap_ssl: document.getElementById('ixSsl').checked,
    imap_user: document.getElementById('ixUser').value.trim(),
  };
  const p = document.getElementById('ixPass').value;
  if (p) body.password = p;
  const r = await api('/api/mailbox/test',{method:'POST',body:JSON.stringify(body)});
  if (r.__http_error){ toast(r.detail||r.error||'测试失败','danger'); return; }
  // 连上了不等于配好了：把"下一步点哪里"直接说出来，省掉一轮猜测
  toast(r.ok?(('连接成功：'+(r.message||''))+(r.next_step?(' ｜ '+r.next_step):''))
            :('连接失败：'+(r.error||r.message||'')), r.ok?'ok':'danger');
  const out = document.getElementById('mailPreview');
  if (out && r.ok && r.next_step){
    out.innerHTML = `<div class="ok">${esc(r.message||'')}<div class="small" style="margin-top:4px">${esc(r.next_step)}</div></div>`;
  }
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
      <td>${esc(d.name||'—')}</td><td>${esc(d.tier||'—')}</td>
      <td>${d.score==null?'—':d.score}</td>
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
    <table><thead><tr><th>文件</th><th>结果</th><th>姓名</th><th>档位</th><th>评分</th><th>说明</th></tr></thead>
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
        ${m.has_resume?'<span class="chip" style="color:#00b42a;background:#e8ffea">含简历</span>':''}</td>
      </tr>`).join('')||'<tr><td colspan="4">邮箱里没有邮件</td></tr>'}</tbody></table>`;
}

/* ------------------------------ 岗位管理 ------------------------------ */
async function viewOrg(){
  const j = await api('/api/jobs');
  const jobs = j.items || [];
  document.getElementById('view').innerHTML = `
  <div class="card">
    <h2>岗位管理</h2>
    <div class="note" style="margin-top:8px">JD 就是这个岗位的评分尺子：简历入库时按它算分。
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
    <table><thead><tr><th>岗位</th><th>JD（评分尺子）</th><th class="nw">投递数</th><th class="nw">状态</th><th class="nw">操作</th></tr></thead>
      <tbody>${jobs.map(x=>`<tr>
        <td>${esc(x.title)}</td>
        <td class="small jdsum">${jdSummary(x.jd_json)}</td>
        <td class="nw">${x.applications_count||0}</td>
        <td class="nw">${x.active?'<span class="chip" style="color:#0a7f1f;background:#e8ffea">开放</span>':'<span class="chip" style="color:#86909c;background:#f2f3f5">已停用</span>'}</td>
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
// JD 摘要：列表里一眼看出这个岗位的评分尺子是什么
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
    : '<span style="color:#86909c">沿用默认尺子</span>';
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
  return `<div class="note">JD 是评分尺子。<b>保存后只影响之后新入库的投递评分，已入库的档位保持不变。</b></div>
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
        <span class="chip" style="color:#86909c;background:#f2f3f5">HR 已确认，未改动</span></span>`;
    } else if (x.changed){
      diff = `<b style="color:${tcol(x.old_tier)}">${esc(x.old_tier||'—')}</b>
        → <b style="color:${tcol(x.new_tier)}">${esc(x.new_tier||'—')}</b>
        <span class="small">（建议档位${r.applied?'已更新':'将更新'}）</span>`;
    } else {
      diff = '<span class="small" style="color:#86909c">无变化</span>';
    }
    const why = x.changed && (x.reasons||[]).length
      ? `<div class="small">${esc((x.reasons||[]).slice(0,2).join('；'))}</div>` : '';
    const note = x.note ? `<div class="small warn-txt">${esc(x.note)}</div>` : '';
    return `<tr>
      <td><b>${esc(x.name||'未识别')}</b><div class="small">投递 #${x.application_id}</div></td>
      <td class="nw">${x.old_score==null?'—':x.old_score} → ${x.new_score==null?'—':x.new_score}</td>
      <td class="nw">${diff}</td>
      <td>${why}${note}</td></tr>`;
  }).join('');
  document.getElementById('mBody').innerHTML = `
    <div class="srcbox">岗位 <b>${esc((r.job||{}).title||'')}</b>
      ｜尺子来源 ${esc(r.jd_source||'')}
      ｜共 ${r.total||0} 条：建议档位${r.applied?'变化':'将变化'} <b>${r.changed||0}</b> 条、
      已确认未改动 ${r.kept_hr_confirmed||0} 条、无法重算 ${r.cannot_regrade||0} 条</div>
    <div class="${r.changed? 'warn':'ok'}" style="margin-top:10px">${esc(r.note||'')}</div>
    <table style="margin-top:12px"><thead><tr><th>候选人</th><th class="nw">评分</th>
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
  toast(jdFilled?'岗位已创建，JD 已写入评分尺子':'岗位已创建（JD 沿用默认尺子）','ok');
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
async function viewProps(){
  const [p, a] = await Promise.all([api('/api/proposals'), api('/api/audit?limit=40')]);
  document.getElementById('view').innerHTML = `
  <div class="panel"><h2>待确认提案</h2>
    <div class="note">这些是智能体提出的写操作。<b>在你确认之前，人才库没有任何改动。</b></div>
    <div class="spacer"></div>
    <table><thead><tr><th>#</th><th>类型</th><th>内容</th><th>风险</th><th>状态</th>
      <th>提交时间</th><th>操作</th></tr></thead>
      <tbody>${(p.items||[]).map(x=>`<tr>
        <td>${x.id}</td><td>${esc(x.tool)}</td><td>${esc(x.summary)}</td>
        <td>${esc(x.risk)}</td><td>${esc(x.status)}</td><td class="small">${esc(x.created_at||'')}</td>
        <td>${x.status==='待确认'?`<button class="btn-ok"
            onclick="decide(${x.id},'approve')">确认执行</button>
          <button class="btn-danger"
            onclick="decide(${x.id},'reject')">拒绝</button>`:'—'}</td>
        </tr>`).join('')||'<tr><td colspan="7">暂无提案</td></tr>'}</tbody></table>
  </div>
  <div class="card"><h2>操作审计（最近 40 条）</h2>
    <div class="note">谁在何时看了谁的简历、改了什么档、确认了什么提案，全部留痕。</div>
    <div class="spacer"></div>
    <table><thead><tr><th>时间</th><th>对象</th><th>动作</th><th>变更前</th><th>变更后</th>
      <th>操作人</th></tr></thead>
      <tbody>${(a.items||[]).map(r=>`<tr>
        <td class="small">${esc(r.ts)}</td><td>${esc(r.entity)}#${esc(r.entity_id)}</td>
        <td>${esc(r.action)}</td><td class="small">${esc(r.before)}</td>
        <td class="small">${esc(r.after)}</td><td>${esc(r.operator)}</td>
        </tr>`).join('')||'<tr><td colspan="6">—</td></tr>'}</tbody></table></div>`;
}
async function decide(pid, decision){
  const r = await api('/api/proposals/'+pid+'/decide', {method:'POST', body:JSON.stringify({decision:decision})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'处理失败','danger'); return; }
  toast(decision==='approve' ? ('提案 #'+pid+' 已执行并生效') : ('提案 #'+pid+' 已拒绝'), 'ok');
  await boot();
}

/* ------------------------------ 检索 ------------------------------ */
async function viewSearch(){
  const s = META.search || {};
  document.getElementById('view').innerHTML = `
  <div class="panel">
    <div class="flexbetween">
      <div><h2 style="margin:0">人才检索</h2>
        <div class="note">技能召回走本体精确匹配（结果可解释、带原文证据）；
          语义召回用于说不清关键词的探索性需求。
          当前索引模型 <b>${esc(s.index_model||'未建立')}</b>，已索引 ${s.indexed||0}/${s.people||0} 人。
          导入新简历后会自动建立增量索引（只处理新增/变更的人），无需手动重建。</div></div>
      <div class="bar"><button onclick="go('import')">导入与来源</button>
        <button onclick="refresh()">刷新索引状态</button>
        <span class="badge" style="background:#f2f3f5;color:#86909c">${esc(s.model||'')}</span></div>
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
      <div style="margin-top:6px">${(x.skills||[]).map(k=>`<span class="chip" style="color:#3370ff;background:#e8f0ff">${esc(k)}</span>`).join('')}</div>
      ${Object.entries(x.evidence||{}).map(([k,v])=>`<div class="ev">证据[${esc(k)}]：${esc(v)}</div>`).join('')}
      <div class="acts"><button onclick="showDetail(${x.candidate_id})">完整档案</button></div>
      </div>`).join('')||'<div class="card" style="margin-top:8px">没有同时具备这些技能的人。可切换为「具备其一」再试。</div>'}`;
}
async function doSemSearch(){
  const q = document.getElementById('semInput').value || '';
  const out = document.getElementById('semOut');
  if (!q.trim()){ out.innerHTML = '<div class="warn">请输入检索语句</div>'; return; }
  out.innerHTML = '<div class="note">检索中…</div>';
  const r = await api('/api/search/semantic?q='+encodeURIComponent(q)+'&top_k=8');
  if (r.__http_error){ out.innerHTML = '<div class="danger">'+esc(r.detail||'失败')+'</div>'; return; }
  out.innerHTML = `<div class="note">索引模型 ${esc(r.model||'—')}${r.error?('｜'+esc(r.error)):''}
    ${r.note?('｜'+esc(r.note)):''}</div>
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

/* ------------------------------ 邮箱配置 ------------------------------ */
async function viewMailCfg(){
  const c = await api('/api/mailbox/config');
  const attExt = (c.attachment_ext||[]).join(',');
  document.getElementById('view').innerHTML = `
  <div class="panel">
    <h2>邮箱配置</h2>
    <div class="note">用于把投递到招聘邮箱的简历自动收进人才库。IMAP 只读增量拉取，不删信、不改已读。</div>
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
    </div>
    <div id="cfgOut"></div>
    <div class="note" style="margin-top:10px">口令只会写入本地 config/imap.secret（权限 0600），不会显示在界面或日志里，也不提交到版本库。</div>
  </div>`;
  loadCfgPresets();
  showCfgWarnings();
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
async function viewSys(){
  const [pol, onto, st] = await Promise.all([
    api('/api/policy'), api('/api/ontology'), api('/api/settings')]);
  const a = pol.access || {}, pii = a.pii_protection || {};
  const gOn = !!st.gender_filter_enabled;
  document.getElementById('view').innerHTML = `
  <div class="panel"><h2>红线（写死在设计里）</h2>
    ${(pol.red_lines||[]).map(x=>`<div class="ok" style="margin-bottom:6px">${esc(x)}</div>`).join('')}
  </div>
  <div class="card">
    <div class="flexbetween">
      <h2 style="margin:0">性别标签与筛选（默认关闭）</h2>
      <label style="display:flex;align-items:center;gap:7px;font-size:14px">
        <input type="checkbox" id="gToggle" ${gOn?'checked':''} onchange="setGenderFilter(this.checked)">
        <b>${gOn?'已开启':'已关闭'}</b></label>
    </div>
    <div class="note" style="margin-top:8px">校招场景下简历普遍写明性别，系统会把简历上<b>明写的标签行</b>
      作为展示信息（如「性别：女」）；<b>不做任何推断</b>，简历没写就留空。
      <br><b>性别永不参与评分与分级</b>——评分函数只读学历、年限、技能、证书。
      <br>筛选开关<b>默认关闭</b>：依据《就业促进法》第 27 条、《妇女权益保障法》第 43 条，
      招聘不得限定性别；需要按性别分组查看时由 HR 主动打开，<b>开启动作写入审计</b>。</div>
    <div class="srcbox" style="margin-top:10px">${esc(st.note||'')}<div class="small" style="margin-top:4px">${esc(st.policy||'')}</div></div>
  </div>
  <div class="card"><h2>访问控制</h2>
    <div class="note">单角色 <b>招聘 HR</b>（具备全部权限），无角色切换、无盲筛。<br>
      ${esc(a.contact_note||'')}<br>
      存储算法：${esc(pii.algorithm||'')}（${esc(pii.key_source||'')}）
      ${pii.degraded?'<span style="color:#f53f3f"> — 降级中，未加密！</span>':'<span style="color:#00b42a">正常运行</span>'}<br>
      不采集：${esc(a['不采集']||'')}<br>
      性别处理：${esc(a['性别处理']||'—')}<br>
      审计规则：${esc(a.audit_rule||'')}<br>${esc(a.retention_note||'')}</div>
  </div>
  <div class="card"><h2>合规屏蔽（进入模型之前的文本）</h2>
    <div class="note">屏蔽类别：${(pol.sensitive_scrub||{}).屏蔽类别?.join('、')||'—'}
      <br>依据：${esc((pol.sensitive_scrub||{}).依据||'')}
      <br>作用范围：${esc((pol.sensitive_scrub||{}).作用范围||'')}
      <br>原件留存：${esc((pol.sensitive_scrub||{}).原件留存||'')}</div>
  </div>
  <div class="card"><h2>技能本体</h2>
    <div class="note">版本 ${esc(onto.describe?.version||'—')} ·
      条目 ${onto.describe?.canonical_count||0} 条 · 可匹配写法 ${onto.describe?.alias_count||0} 个 ·
      分布 ${esc(JSON.stringify(onto.describe?.categories||{}))}</div>
  </div>
  <div class="card"><h2>扩展机制：学科目录与领域包</h2>
    <div class="note">
      <b>新增一个行业的岗位，不需要改代码。</b>分工是三块：
      <div class="kv" style="margin-top:8px">
        <div class="k">学科目录</div><div id="majorInfo">加载中…</div>
        <div class="k">领域包</div><div id="domainInfo">加载中…</div>
      </div>
      <div class="small" style="margin-top:8px">
        <b>技能是开放集合、永远补不完；学科是有限目录、一次建成。</b>
        所以「技能对不上」不再等于「判不了」：未收录项会走文字比对通道，
        并把<b>置信度</b>与<b>未收录条数</b>一起摆出来，而不是给出一个看起来正常的错误结论。
        领域包（<code>config/domains/*.json</code>）就是"某个行业的技能词表"，
        导入前会先备份本体到 <code>data/backup/</code>，并逐条报告别名迁移。
      </div>
      <div class="bar" style="margin-top:8px">
        <button onclick="loadExtensionInfo(true)">刷新</button>
        <span class="small">命令行等价操作：<code>cli.py domains</code> / <code>cli.py majors</code> / <code>cli.py import-domain 财务</code></span>
      </div>
    </div>
  </div>
  <div class="card"><h2>智能体工具</h2>
    <div class="kv">
      <div class="k">读（开放）</div><div>${esc((META.tools.read||[]).join('、'))}</div>
      <div class="k">算（开放）</div><div>${esc((META.tools.compute||[]).join('、'))}</div>
      <div class="k">写（需确认）</div><div>${esc((META.tools.write_requires_confirmation||[]).join('、'))}</div>
      <div class="k">外发（禁用）</div><div>${esc((META.tools.disabled||[]).join('、'))}</div>
    </div>
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
  <div class="card"><h2>运行环境</h2>
    <div class="kv">
      <div class="k">对话模型</div><div>${esc(META.model.model||'—')} ·
        ${META.model.reachable?'服务可达':'服务不可达'} · ${META.model.model_installed?'模型已安装':'模型未安装'}
        ${META.model.base_url?('<br><span class="small">'+esc(META.model.base_url)+'</span>'):''}
        ${META.model.error?('<br><span class="small">'+esc(META.model.error)+'</span>'):''}</div>
      <div class="k">向量模型</div><div>${esc(META.search.model||'—')} · ${META.search.dim||0} 维 ·
        ${META.search.reachable?'服务可达':'<span style="color:#f53f3f">服务不可达（降级中）</span>'} ·
        已索引 ${META.search.indexed||0} 人
        ${(META.search.index_model && META.search.index_model !== META.search.model)
          ? ('<br><span class="small">索引实际使用 <b>'+esc(META.search.index_model)+'</b>'
             + '——配置的向量模型没连上时自动退回本地哈希向量，检索质量会下降</span>')
          : ''}
        ${META.search.error?('<br><span class="small">'+esc(META.search.error)+'</span>'):''}</div>
      <div class="k">解析能力</div><div>PyMuPDF ${META.parse.pymupdf?'✓':'✗'} ·
        MarkItDown ${META.parse.markitdown?'✓':'✗'} · OCR ${META.parse.ocr?'✓':'✗（图片简历将标『待人工判读』）'}</div>
      <div class="k">邮箱接入</div><div>模式 ${esc(META.mailbox.mode||'—')} ·
        只读 ${META.mailbox.readonly?'✓':'✗'} · 附件白名单 ${esc((META.mailbox.attachment_ext||[]).join(' '))}
        · 单个附件上限 ${META.mailbox.max_attachment_mb==null?20:META.mailbox.max_attachment_mb} MB
        · 同岗重复投递归并为新版本（${META.mailbox.same_job_reapply_days} 天内）</div>
      <div class="k">性别标签</div><div>
        ${META.settings && META.settings.gender_filter_enabled
          ? '<span style="color:#a45a00">筛选开关已开启</span>（开启动作已留痕）'
          : '仅展示，筛选开关默认关闭'} ·
        不参与评分与分级</div>
      <div class="k">岗位</div><div>${(META.jobs||[]).length} 个（含已停用）</div>
    </div>
  </div>`;
  loadExtensionInfo();
}
// 扩展机制信息（学科目录 + 领域包）：只读展示，让人知道"加新行业"的入口在哪。
// 刻意不在这里放"一键导入"：导入会改写全院共用的技能本体，属于口径级动作，
// 走命令行 `cli.py import-domain` 或接口的"先预演再落盘"两步，比在设置页点一下更稳。
async function loadExtensionInfo(force){
  const mi = document.getElementById('majorInfo');
  const di = document.getElementById('domainInfo');
  if(!mi || !di) return;
  if(!force && di.dataset.loaded==='1') return;
  const [mj, dm] = await Promise.all([api('/api/majors?limit=1'), api('/api/domains')]);
  if(!mj.__http_error && mj.info){
    const by = mj.info.by_category || {};
    const top = Object.entries(by).sort((a,b)=>b[1]-a[1]).slice(0,6)
      .map(([k,v])=>k+' '+v).join('、');
    mi.innerHTML = `通用学科目录 <b>v${esc(mj.info.version||'—')}</b> ·
      一级学科 <b>${mj.info.major_count||0}</b> 个 · 可匹配写法 <b>${mj.info.matchable_count||0}</b> 条
      <br><span class="small">门类：${esc(mj.info.categories?.join('、')||'—')}<br>分布：${esc(top)}…</span>`;
  } else {
    mi.textContent = '读取失败';
  }
  if(!dm.__http_error && dm.items){
    di.innerHTML = (dm.items.length
      ? `<b>${dm.items.length}</b> 个可导入：`
        + dm.items.map(p=>`「${esc(p.name)}」${p.skill_count} 条技能`
            + (p.new_categories?.length?`<span style="color:#a45a00">（含新大类 ${esc(p.new_categories.join('、'))}）</span>`:'')
          ).join('、')
        + `<br><span class="small">目录：<code>${esc(dm.dir||'config/domains')}</code>；
           导入前先预演：<code>cli.py import-domain 财务 --dry-run</code></span>`
      : '暂无领域包');
  } else {
    di.textContent = '读取失败';
  }
  di.dataset.loaded = '1';
}
// 性别筛选开关：默认关。打开是合规敏感动作，所以走确认 + 后端留痕 + 回话说明。
async function setGenderFilter(on){
  const msg = on
    ? '开启后，人才库列表会多出一个「性别」筛选项。\\n\\n'
      + '· 性别只来自简历明写标签，系统不做推断；\\n'
      + '· 性别不参与评分与分级（改档结果与性别无关）；\\n'
      + '· 依据《就业促进法》第 27 条，招聘不得限定性别，请勿将其作为筛除依据；\\n'
      + '· 本次开启会写入审计。\\n\\n确认开启？'
    : '关闭性别筛选？列表将恢复为全部候选人。';
  if (!confirm(msg)){
    const t = document.getElementById('gToggle');
    if (t) t.checked = !on;
    return;
  }
  const r = await api('/api/settings', {method:'POST',
    body:JSON.stringify({gender_filter_enabled:!!on})});
  if (r.__http_error || r.error){ toast(r.detail||r.error||'设置失败','danger'); return; }
  GENDER = '';
  toast(r.note||'设置已更新', on?'warn':'ok');
  META = await api('/api/meta');
  refresh();
}

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


def render_page(auth_enabled: bool = False) -> str:
    return (_PAGE
            .replace("__UI_BUILD__", _UI_BUILD)
            .replace("__AUTH_ENABLED__", "true" if auth_enabled else "false"))
