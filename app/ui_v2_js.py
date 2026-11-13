"""v2 界面脚本（从 ui_v2.py 拆出，避免单文件无限膨胀）。"""

_JS = r"""
var ITEMS = [], CUR = null, CUR_DETAIL = null;

/* ---------- 登录态（2026-10-20 补齐） ----------
   v2 之前**只声明了"不支持"**：页面里没有 AUTH_ENABLED、没有 TOKEN，
   `api()` 也就从不注入 X-TP-Token —— 设了 TP_AUTH=1 时全页接口都 401，
   而界面除了顶部一行提示之外没有任何别的线索（`render_page_v2` 里那段）。
   这里照原版（`ui.py:658/700/892/1094`）补齐同一套：
     · AUTH_ENABLED 由服务端按 TP_AUTH 注入（本机模式为 false，请求头照旧不���）
     · TOKEN 取 localStorage.tp_token（登录成功后由谁写入，沿用原版口径）
     · hdr() 给 api() 与裸 fetch 统一加头
     · fileUrl() 供**浏览器原生导航**用（`<img>`/window.open/a[href] 带不了
       请求头）：登录模式下先向 /api/files/sign 换一张短时、绑定资源的票据，
       URL 里不再出现长期会话令牌。 */
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
/* 原生导航（window.open / a[href] / img）带不了请求头，登录模式下向服务端
   换一张**短时、绑定资源**的票据，而不是把长期会话令牌拼进 URL。
   本机模式免登录，直接用原 URL。 */
async function fileUrl(url, kind, opts){
  if(!(AUTH_ENABLED && TOKEN)) return url;
  var body = {kind: kind};
  if(opts) for(var k in opts){
    if(Object.prototype.hasOwnProperty.call(opts, k)) body[k] = opts[k];
  }
  var r = await api('/api/files/sign', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  return (r && r.url) ? r.url : url;
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
/* 错误值可能是字符串、FastAPI 的 detail 数组、或普通对象。
   直接丢给 textContent 会显示成 `[object Object]`——先归一成一句人话。 */
function errText(v){
  if(v === null || v === undefined) return '';
  if(typeof v === 'string') return v;
  if(Array.isArray(v)){
    return v.map(function(x){
      if(typeof x === 'string') return x;
      if(x && typeof x === 'object') return x.msg || x.detail || x.message || JSON.stringify(x);
      return String(x);
    }).filter(Boolean).join('；');
  }
  if(typeof v === 'object'){
    return v.detail || v.message || v.error || JSON.stringify(v);
  }
  return String(v);
}
/* 智能助手生成的「下载名单」链接：登录模式下 <a> 带不上请求头，
   这里拦下来用 fetch 带 token 下载，避免点开新标签页只看到 401。 */
document.addEventListener('click', function(ev){
  var a = ev.target && ev.target.closest
    ? ev.target.closest('a[href^="/api/export/roster"],a[href^="/api/export/interview-schedule"]')
    : null;
  if(!a) return;
  ev.preventDefault();
  toast('正在导出…');
  var href = a.getAttribute('href') || '';
  var fallback = href.indexOf('interview-schedule') >= 0
    ? ('面试日程-' + todayStamp() + '.xlsx')
    : ('候选人名单-' + todayStamp() + '.xlsx');
  fetch(href, {headers: hdr({})})
    .then(function(r){
      if(!r.ok){ throw new Error('HTTP ' + r.status); }
      return r.blob().then(function(blob){
        return {blob: blob, name: exportFilename(r, fallback)};
      });
    })
    .then(function(x){ saveBlob(x.blob, x.name); toast('已导出', 'ok'); })
    .catch(function(e){ toast('导出失败：' + e.message, 'danger'); });
});
function toast(msg, kind){
  var t = document.getElementById('toast');
  if(!t) return;
  t.textContent = errText(msg);
  t.className = 'toast on ' + (kind || 'info');
  clearTimeout(t._h); t._h = setTimeout(function(){ t.classList.remove('on'); }, 2200);
}
/* 让非 button 的可点标签（× 等）也能用 Enter/Space 触发 */
function keyClick(ev){
  if(ev.key === 'Enter' || ev.key === ' '){
    ev.preventDefault();
    ev.currentTarget.click();
  }
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
    /* 422 的 detail 是对象数组（Pydantic），归一后 toast/页面才读得懂 */
    if(j.__err) j.__err = errText(j.__err);
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
  PAGE.page = 1;
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
  PAGE.page = 1;
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
  if(SEL_MENU && SEL_MENU.contains(t)) return;
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
  /* v4.0.17：面试结论只有未判/拟录用/不录用；签约状态在档案与面试名单里看。 */
  fillSel('fOutcome', '面试结论', [['拟录用','拟录用'],['不录用','不录用'],['未判','未判']]);
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
      PAGE.page = 1;
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
            '<button class="pg" data-pipe-page="' + esc(k) + '" data-delta="-1"' +
              (cur <= 1 ? ' disabled' : '') + '>‹</button>' +
            '<span class="num">' + cur + '/' + pages + '</span>' +
            '<button class="pg" data-pipe-page="' + esc(k) + '" data-delta="1"' +
              (cur >= pages ? ' disabled' : '') + '>›</button></div>'
        : '') +
    '</div>';
  }).join('');
  /* 阶段名可能来自配置（含引号），不能拼进 inline onclick。 */
  Array.prototype.forEach.call(box.querySelectorAll('[data-pipe-page]'), function(b){
    b.addEventListener('click', function(){
      pipePage(b.getAttribute('data-pipe-page'), Number(b.getAttribute('data-delta')) || 0);
    });
  });
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
/* v4.3.0：批量操作菜单（初筛通过 / 批量归岗）与「批量维护」同一套开合规矩 */
function placeBatchOpsPanel(){
  var p = document.getElementById('batchOpsPanel');
  var b = document.getElementById('btnBatchOps');
  if(!p || !b || !p.classList.contains('on')) return;
  var r = b.getBoundingClientRect();
  var w = p.offsetWidth || 180;
  var left = Math.min(r.left, window.innerWidth - w - 8);
  p.style.left = Math.max(8, left) + 'px';
  p.style.top = Math.round(r.bottom + 4) + 'px';
}
function toggleBatchOpsMenu(){
  var p = document.getElementById('batchOpsPanel');
  var m = document.getElementById('morePanel');
  var b = document.getElementById('batchPanel');
  if(m) m.classList.remove('on');
  if(b) b.classList.remove('on');
  if(!p) return;
  p.classList.toggle('on');
  if(p.classList.contains('on')) placeBatchOpsPanel();
}
window.addEventListener('resize', placeBatchOpsPanel);
window.addEventListener('scroll', placeBatchOpsPanel, true);
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
  if(SEL_MENU && SEL_MENU.contains(ev.target)) return;
  if(panel.contains(ev.target) || (more && more.contains(ev.target))) return;
  panel.classList.remove('on');
}, true);
/* 「批量维护」同一套规矩：点面板外或按 Esc 收起，点面板内不关。 */
document.addEventListener('click', function(ev){
  var panel = document.getElementById('batchPanel');
  if(!panel || !panel.classList.contains('on')) return;
  var btn = document.getElementById('btnBatch');
  if(SEL_MENU && SEL_MENU.contains(ev.target)) return;
  if(panel.contains(ev.target) || (btn && btn.contains(ev.target))) return;
  panel.classList.remove('on');
}, true);
document.addEventListener('click', function(ev){
  var panel = document.getElementById('batchOpsPanel');
  if(!panel || !panel.classList.contains('on')) return;
  var btn = document.getElementById('btnBatchOps');
  if(SEL_MENU && SEL_MENU.contains(ev.target)) return;
  if(panel.contains(ev.target) || (btn && btn.contains(ev.target))) return;
  panel.classList.remove('on');
}, true);
document.addEventListener('keydown', function(ev){
  if(ev.key !== 'Escape') return;
  var panel = document.getElementById('morePanel');
  if(panel) panel.classList.remove('on');
  var batch = document.getElementById('batchPanel');
  if(batch) batch.classList.remove('on');
  var ops = document.getElementById('batchOpsPanel');
  if(ops) ops.classList.remove('on');
});
function clearFilters(){
  ['kw','fEdu','fStage','fTier','fGender','fUniv','fBachelor','fCet','fOutcome',
   'fOverseas','fJob','fUnit'].forEach(function(id){
    var el = document.getElementById(id);
    if(!el) return;
    if(el.multiple){   // 多选下拉必须逐个取消选中，赋空串清不掉
      Array.prototype.forEach.call(el.options, function(o){ o.selected = false; });
    }else{
      el.value = '';
    }
  });
  /* 投递时间是两个日期框，清空后还要切回 text，否则会留下 yyyy/mm/dd 的原生占位 */
  ['fDateFrom','fDateTo'].forEach(function(id){
    var el = document.getElementById(id);
    if(el){ el.value = ''; el.type = 'text'; }
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
  PAGE.page = 1;
  load();
}

/* ---------------- 筛选状态同步到 URL（刷新/后退不丢） ---------------- */
var URL_READY = false;
function urlState(){
  var p = new URLSearchParams();
  [['kw','kw'],['education','fEdu'],['stage','fStage'],['gender','fGender'],
   ['univ','fUniv'],['bachelor','fBachelor'],['cet','fCet'],['outcome','fOutcome'],
   ['overseas','fOverseas'],['job','fJob'],['interview_unit','fUnit'],
   ['date_from','fDateFrom'],['date_to','fDateTo']].forEach(function(kv){
    var el = document.getElementById(kv[1]);
    var v = el ? String(el.value || '').trim() : '';
    if(v) p.set(kv[0], v);
  });
  if(TAB && TAB !== 'ALL') p.set('tier', TAB);
  if(PAGE.page && PAGE.page > 1) p.set('page', PAGE.page);
  if(PAGE.page_size && PAGE.page_size !== 50) p.set('page_size', PAGE.page_size);
  return p;
}
function syncUrlState(push){
  if(!URL_READY) return;
  var qs = urlState().toString();
  /* hash 也要保留：只拼 path+search 会把 #pool/#mail 抹掉，
     刷新后 enterDefaultView() 就回落到默认的智能助手。 */
  var want = location.pathname + (qs ? '?' + qs : '') + (location.hash || '');
  if(want === location.pathname + location.search + location.hash) return;
  try{ history[push ? 'pushState' : 'replaceState'](null, '', want); }catch(e){}
}
/* 勾选式多选（学历/四六级）从 URL 恢复：隐藏 input 和复选框都要同步 */
function msSetCsv(id, csv){
  var m = MSEL[id];
  if(!m) return;
  var vals = String(csv || '').split(',').map(function(s){ return s.trim(); })
    .filter(Boolean);
  m.sel = vals.filter(function(v){
    return m.pairs.some(function(p){ return p[0] === v; });
  });
  var wrap = document.getElementById(id);
  if(wrap) Array.prototype.forEach.call(wrap.querySelectorAll('input[type=checkbox]'),
    function(c){
      var p = m.pairs[Number(c.dataset.i)];
      c.checked = !!p && m.sel.indexOf(p[0]) >= 0;
    });
  msSync(id);
}
function applyUrlState(){
  var p = new URLSearchParams(location.search);
  /* 先把所有控件复位到默认，再按 URL 赋值 —— 否则浏览器"后退"到一个
     不带筛选的地址时，上一页的条件会残留在控件里。 */
  ['kw','fStage','fGender','fUniv','fBachelor','fOutcome','fOverseas','fUnit','fJob']
    .forEach(function(id){
      var el = document.getElementById(id);
      if(el) el.value = '';
    });
  ['fDateFrom','fDateTo'].forEach(function(id){
    var el = document.getElementById(id);
    if(el){ el.value = ''; el.type = 'text'; }
  });
  msSetCsv('msEdu', '');
  msSetCsv('msCet', '');
  TAB = 'ALL';
  Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function(x){
    x.classList.toggle('on', x.dataset.tab === 'ALL');
  });
  PAGE.page = 1;
  PAGE.page_size = 50;
  var set = function(id, key){
    var el = document.getElementById(id);
    if(el && p.has(key)) el.value = p.get(key);
  };
  set('kw','kw'); set('fStage','stage'); set('fGender','gender');
  set('fUniv','univ'); set('fBachelor','bachelor'); set('fOutcome','outcome');
  set('fOverseas','overseas');
  /* 岗位选项来自异步 /api/jobs；这里先记下 URL 值，选项就绪后再选中。
     先清掉上一页残留的 pending（后退到无 job 的地址时必须清空）。 */
  var jobEl = document.getElementById('fJob');
  if(jobEl){
    delete jobEl.dataset.pending;
    var jv = p.get('job');
    if(jv){
      /* 选项已加载就直接选中；还没加载（首次启动）就留给 fillJobSel。 */
      var hasJob = Array.prototype.some.call(jobEl.options, function(o){
        return o.value === jv;
      });
      if(hasJob) jobEl.value = jv; else jobEl.dataset.pending = jv;
    }
  }
  /* 面试部门的选项来自数据/字典，启动时可能还没有这一项；先把 URL 里的值
     补成一个 option 再选中，否则 `.value = ...` 会被浏览器静默重置成空。 */
  var uEl = document.getElementById('fUnit');
  if(uEl && p.has('interview_unit')){
    var uv = p.get('interview_unit');
    var has = Array.prototype.some.call(uEl.options, function(o){ return o.value === uv; });
    if(!has){
      var o = document.createElement('option');
      o.value = uv; o.textContent = uv; uEl.appendChild(o);
    }
    uEl.value = uv;
  }
  [['fDateFrom','date_from'],['fDateTo','date_to']].forEach(function(kv){
    var el = document.getElementById(kv[0]);
    if(el && p.has(kv[1])){ el.type = 'date'; el.value = p.get(kv[1]); }
  });
  if(p.has('education')) msSetCsv('msEdu', p.get('education'));
  if(p.has('cet')) msSetCsv('msCet', p.get('cet'));
  if(p.has('tier')){
    TAB = p.get('tier');
    Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function(x){
      x.classList.toggle('on', x.dataset.tab === TAB);
    });
  }
  var ps = Number(p.get('page_size')); if(ps > 0) PAGE.page_size = ps;
  var pg = Number(p.get('page')); if(pg > 0) PAGE.page = pg;
}
window.addEventListener('popstate', function(){
  applyUrlState();
  load({push:false});
});
/* 加载态：顶部细进度条 + 列表/分页变淡；同时标记 aria-busy */
function setListBusy(on){
  document.body.classList.toggle('list-busy', !!on);
  var bar = document.getElementById('loadBar');
  if(bar) bar.classList.toggle('on', !!on);
  var list = document.getElementById('list');
  if(list) list.setAttribute('aria-busy', on ? 'true' : 'false');
}
/* 窄屏左侧名单抽屉 */
function toggleListDrawer(){
  document.body.classList.toggle('list-open');
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
  /* 岗位选项是异步加载的：URL 里的 job 先落在 dataset.pending，
     选项到达前 fJob.value 还是空。只看 value 会把筛选漏掉。 */
  var _jobEl = document.getElementById('fJob');
  var _job = _jobEl ? (_jobEl.value || _jobEl.dataset.pending || '') : '';
  if(_job) p.push('job=' + encodeURIComponent(_job));
  add('interview_unit','fUnit');
  if(TAB !== 'ALL') p.push('tier=' + encodeURIComponent(TAB));
  /* 投递时间：具体日期的起止范围。只填一端就只卡一端；
     两端都填且顺序反了，自动对调（日期是 YYYY-MM-DD，字典序即时间序）。 */
  var dateFrom = document.getElementById('fDateFrom').value;
  var dateTo = document.getElementById('fDateTo').value;
  if(dateFrom && dateTo && dateFrom > dateTo){
    var swap = dateFrom; dateFrom = dateTo; dateTo = swap;
  }
  if(dateFrom) p.push('date_from=' + encodeURIComponent(dateFrom));
  if(dateTo) p.push('date_to=' + encodeURIComponent(dateTo));
  return p.length ? '?' + p.join('&') : '';
}

/* ---------- 左侧列表 ---------- */
/* 阶段用**各阶段自己的颜色**（config/stages.json 原版就定义好了）：
   新投递 灰蓝 / 初筛通过 蓝 / 待面试 紫 / 已面试 青绿 / 已发放offer 橙 / 已结束 灰。
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
  var nm = esc(x.name || '未识别');
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
      ' data-avatar-url="' + esc(x.avatar_url || '') + '"' +
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
  return '<div class="row' + (CUR === x.id ? ' on' : '') + '" data-id="' + x.id + '"' +
    ' role="button" tabindex="0" aria-label="打开 ' + nm + ' 的档案">' +
    '<div class="rowbar"></div>' +
    '<div class="chk"><input type="checkbox" data-id="' + x.id + '"' +
      (SEL[x.id] ? ' checked' : '') + '></div>' +
    '<div class="av">' + esc((x.name || '?').slice(0,1)) + av + '</div>' +
    '<div style="min-width:0">' +
      '<div class="nm">' + nm + stageChip(x.stage) + outcomeChip(x.outcome, x.stage) +
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
/* 分页：**默认一页 50 人**（业务方 2026-11-02 指定；原来的 25 一眼看不完一个岗位的投递）。
   注意仍然要**显式传 page_size**——不传的话服务端返回全量，页数算出来永远是 1/1，翻页是假的。 */
var PAGE = {page:1, page_size:50, total:0, total_pages:1};
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
    '<button class="btn sm pgsel" id="pgSelAll" onclick="togglePageSelect()" ' +
      'title="勾选当前页的全部候选人">' +
      (pageAllSelected() ? '取消全选' : '全选本页') + '</button>' +
    '<span class="pgbreak"></span>' +
    '<span class="pgjumpwrap"><span class="hint">跳至</span>' +
    '<input class="pgjump" id="pgJump" value="' + cur + '" ' +
      'onkeydown="if(event.key===\'Enter\'){gotoPage(parseInt(document.getElementById(\'pgJump\').value)||1);}" ' +
      'style="width:52px;height:26px;text-align:center">' +
    '<span class="hint">页</span></span>' +
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
  /* 键盘可达：行本身是 role=button，Enter/Space 打开档案；
     落在勾选框/按钮上时不抢它们的键。 */
  el.addEventListener('keydown', function(ev){
    if(ev.key !== 'Enter' && ev.key !== ' ') return;
    var t = ev.target;
    if(t && (t.tagName === 'INPUT' || t.tagName === 'BUTTON' ||
             t.tagName === 'SELECT' || t.tagName === 'TEXTAREA')) return;
    var row = t && t.closest ? t.closest('.row') : null;
    if(row && row.dataset.id){
      ev.preventDefault();
      pick(Number(row.dataset.id));
    }
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
  var url = im.dataset.avatarUrl || ('/api/candidates/' + cid + '/avatar');
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
/* 请求代际：快速点 A→B 时，A 的旧响应不能覆盖 B（详情/概览/写邮件收件人）。 */
var DETAIL_SEQ = 0;
var CAND_SEQ = 0;
var COMPARE_MAX = 10;
var COMPARE_IDS = [];
var COMPARE_DATA = {};
var COMPARE_ACTIVE = null;

/* ---------- 概览 ---------- */
async function showOverview(){
  var seq = ++DETAIL_SEQ;
  MODE = 'overview'; CUR = null;
  COMPARE_IDS = []; COMPARE_DATA = {}; COMPARE_ACTIVE = null;
  renderList();
  var box = document.getElementById('detail');
  box.classList.remove('comparing');
  box.innerHTML = '<div class="hint">读取概览…</div>';
  /* 两个请求互不依赖，必须并行（v3.0.3）。原来是串行 await，
     等于白白多等一个完整往返 —— 而这是**打开页面第一眼**看到的东西。 */
  var _ov = await Promise.all([api('/api/stats'), api('/api/brief/today'), api('/api/jobs')]);
  /* 概览晚返回不能覆盖用户刚打开的档案。 */
  if(seq !== DETAIL_SEQ || MODE !== 'overview') return;
  var st = _ov[0], br = _ov[1], jobsResp = _ov[2];
  if(st.__err){ box.innerHTML = '<div class="hint">' + esc(st.__err) + '</div>'; return; }
  var tiers = st.tiers || {}, stages = st.stages || {};
  function num(label, v, hint){
    return '<div class="stat"><div class="stat-n num">' + v + '</div>' +
      '<div class="stat-l">' + label + '</div>' +
      (hint ? '<div class="stat-h">' + hint + '</div>' : '') + '</div>';
  }
  var _dismissed = briefDismissedMap();
  var _prios = (br.priorities || []).filter(function(x){
    return !_dismissed[String(x.title || '')];
  });
  window.OV_TODOS = _prios;
  var todo = _prios.map(function(x, i){
    return '<div class="todo" role="button" tabindex="0" data-todo-i="' + i + '">' +
      '<div class="todo-i num">' + (i + 1) + '</div>' +
      '<div style="min-width:0"><div class="todo-t">' + esc(x.title || '') + '</div>' +
      (x.why ? '<div class="todo-w">' + esc(x.why) + '</div>' : '') +
      (x.action ? '<div class="todo-a">' + esc(x.action) + '</div>' : '') + '</div></div>';
  }).join('');
  /* 规则版摘要把第一条待办原样当 headline，下面又列一遍 → 看起来重复。
     只有 headline 确实是一句独立的总结时才显示。 */
  var _head = String(br.headline || '').trim();
  var _first = String(((br.priorities || [])[0] || {}).title || '').trim();
  if(_head && _head === _first) _head = '';
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
        '</div>' +
        '<div class="ov-jobs"><h3>岗位投递人数</h3>' +
          '<div class="ov-jobs-note">按未归档投递数统计</div>' +
          jobBars(jobsResp) + '</div></div>' +
      '<div class="ovcol"><h3>今日待办' +
        (_prios.length ? '（' + _prios.length + '）' : '') + '</h3>' +
        (_head ? '<div class="sum">' + esc(_head) + '</div>' : '') +
        (todo ? '<div class="ov-todos">' + todo + '</div>'
              : '<div class="hint ov-todos">今天没有需要优先处理的事</div>') +
      '</div>' +
    '</div>';
  Array.prototype.forEach.call(box.querySelectorAll('.todo'), function(el){
    function go(){ openTodo(Number(el.dataset.todoI)); }
    el.addEventListener('click', go);
    el.addEventListener('keydown', function(ev){
      if(ev.key === 'Enter' || ev.key === ' '){ ev.preventDefault(); go(); }
    });
  });
  document.getElementById('count').innerHTML =
    '共 <b class="num">' + st.people + '</b> 人';
}
/* 今日待办整行可点：按 action 文案跳到对应的处理入口，
   而不是让 HR 对着一行蓝字自己找。 */
function openTodo(i){
  var p = (window.OV_TODOS || [])[i];
  if(!p) return;
  var text = String(p.action || '') + ' ' + String(p.title || '') + ' ' + String(p.why || '');
  setView('pool');
  if(/投递管道|停滞投递|停滞/.test(text)){
    if(!PIPE_OPEN) togglePipe();
    return;
  }
  if(/缺失字段|信息缺失/.test(text)){
    TAB = 'REVIEW';
    Array.prototype.forEach.call(document.querySelectorAll('#tabs button'), function(b){
      b.classList.toggle('on', b.dataset.tab === 'REVIEW');
    });
    PAGE.page = 1;
    load();
    var list = document.getElementById('list');
    if(list) list.scrollTop = 0;
    return;
  }
  var names = (p.names || []).filter(Boolean);
  if(names.length){
    var hit = (ITEMS || []).filter(function(x){ return names.indexOf(x.name) >= 0; })[0];
    if(hit){ pick(hit.id); return; }
    var kw = document.getElementById('kw');
    if(kw){ kw.value = names[0]; doSearch(); }
    return;
  }
  var kw2 = document.getElementById('kw');
  if(kw2) kw2.focus();
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
      ' data-avatar-url="' + esc(d.avatar_url || '') + '"' +
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
    d.interview_slot ? '时段：' + (schedRangeText(d.interview_date, d.interview_slot,
                                                 d.edu_level) || d.interview_slot) : '',
    d.interview_place ? '地点：' + d.interview_place : '',
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
      '<button class="compare-close" title="关闭这个档案窗口" aria-label="关闭这个档案窗口" ' +
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
        '<button class="pt-x" title="关闭这个档案" aria-label="关闭这个档案" onclick="event.stopPropagation();closeCompareCard(' +
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
/* ---------- 项目经历：按需拉取 + 缓存 ----------
   同一个人可能因面试字典异步到货而二次渲染；用一个共享 Promise，
   避免两次渲染各发一次模型请求。 */
var PROJECT_CACHE = {}, PROJECT_JOBS = {};
function projectFill(wrap, data){
  var html = projectSectionHtml(data);
  if(!html){
    if(wrap && wrap.parentNode){ wrap.parentNode.removeChild(wrap); }
    return;
  }
  wrap.outerHTML = html;
}
async function loadProjects(d, box){
  var key = String(d.id);
  var wrap = box.querySelector('#projectWrap');
  if(!wrap) return;
  if(Object.prototype.hasOwnProperty.call(PROJECT_CACHE, key)){
    projectFill(wrap, PROJECT_CACHE[key]);
    return;
  }
  var timer = setTimeout(function(){
    if(wrap.isConnected){
      wrap.innerHTML = '<h3>项目经历<span class="ai-tag">摘要</span></h3>' +
        '<div class="hint proj-loading">正在从简历中提炼项目经历…</div>';
    }
  }, 350);
  if(!PROJECT_JOBS[key]){
    PROJECT_JOBS[key] = api('/api/candidates/' + d.id + '/projects');
  }
  var r;
  try{ r = await PROJECT_JOBS[key]; }
  catch(e){ r = {__err: String(e)}; }
  clearTimeout(timer);
  delete PROJECT_JOBS[key];
  if(!r || r.__err || !r.ok){
    if(wrap && wrap.parentNode){ wrap.parentNode.removeChild(wrap); }
    return;
  }
  var data = {projects: r.projects || [], direction_summary: r.direction_summary || ''};
  PROJECT_CACHE[key] = data;
  var live = box.querySelector('#projectWrap');
  if(!live || box.dataset.cid !== key) return;
  projectFill(live, data);
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
  ensureHireUnits();
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
  /* 项目经历是 AI 总结、需要调模型：已有缓存同步渲染，没有则异步补上 */
  if(!d.projects_checked) loadProjects(d, box);
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
function projectSectionHtml(p){
  var items = (p && p.projects) || [];
  if(!items.length) return '';
  var dir = String((p && p.direction_summary) || '').trim();
  return '<div class="sec projectsec profile-wide"><h3>项目经历' +
      '<span class="ai-tag">摘要</span></h3>' +
    (dir ? '<div class="proj-dir"><span>项目方向</span><b>' + esc(dir) + '</b></div>' : '') +
    '<div class="proj-list">' + items.map(function(x){
      return '<div class="proj-item">' +
        '<div class="proj-name">' + esc(x.name || '') + '</div>' +
        '<div class="proj-sum" title="' + esc(x.summary || '') + '">' +
          esc(x.summary || '') + '</div>' +
      '</div>';
    }).join('') + '</div></div>';
}
function fullDetail(d){
  var stage = d.stage || '';
  /* 复核状态（2026-11-02 修）：后端把它存在 `applications.status` 里，值为「已确认」，
     **没有** `d.reviewed` 这个字段。原来按钮直接读 d.reviewed → 永远是 undefined →
     永远显示「标记已复核」，用户点完看不出到底复核没复核。 */
  var isReviewed = (d.reviewed === true) || (String(d.status || '') === '已确认');
  /* 详情页头像同样走懒加载（v3.0.3）：与列表用同一套 data-avatar 机制，
     避免"列表已改成懒加载、详情还是一次性请求"两套逻辑漂移。 */
  var av = d.avatar_path === '-' ? ''
    : '<img alt="" data-avatar="' + d.id + '"' +
      ' data-avatar-url="' + esc(d.avatar_url || '') + '"' +
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
  var extractTxt = (extractMode === 'llm+rule' || extractMode === 'llm') ? '模型'
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
      var status = x.raw_text
        ? '<span class="hint" style="margin-left:8px">已识别 ' + esc(String(x.raw_text.length)) + ' 字</span>'
        : ((x.parse_ok === 0 || x.parse_ok === false)
          ? '<span class="hint" style="margin-left:8px">未识别出文字</span>' : '');
      var raw = x.raw_text
        ? '<details class="rawbox" style="margin:2px 0 8px 0"><summary>查看识别全文</summary>' +
          '<pre>' + esc(x.raw_text) + '</pre></details>'
        : '';
      return '<div class="doc"><span class="doc-n">' + esc(x.file_name || '') + status + '</span>' +
        '<span class="doc-a">' + acts + '</span></div>' + raw;
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
    /* 论文/专利条目可能很多：一条一行 + 序号，不能挤成一段不换行的长句。 */
    return '<div class="ev"><b>' + esc(k) + '</b>' +
      '<ol class="honor-list">' +
      rows.map(function(r){ return '<li>' + esc(r) + '</li>'; }).join('') +
      '</ol></div>';
  }).join('') || '<div class="hint">无</div>';
  /* v5.1.1：资格类证书（中级职称/PMP/注册安全工程师…），与荣誉/论文/专利分开。 */
  var certs = d.certificates || [];
  var certHtml = certs.length
    ? '<div class="ev certrow"><b>证书</b>：' +
      certs.map(function(x){ return esc(x); }).join('、') + '</div>'
    : '';
  /* 项目经历：已有 AI 缓存就直接渲染；没有缓存先留一个空挂载点，
     等 `openDetail` 异步拉取（避免打开档案就被模型调用卡住）。 */
  var projHtml = projectSectionHtml(d.projects);
  if(!projHtml && !d.projects_checked && MODE !== 'compare'){
    projHtml = '<div class="sec projectsec profile-wide" id="projectWrap"></div>';
  }

  /* 投递来源（2026-10-28）：来源邮件 + 简历原件合成**一行**，放在档位上方。
     这两件事回答的是同一个问题——"这份档案从哪来的"，原来分两块沉在页面最底部，
     HR 判断"要不要推进"时得来回滚动才能对上是谁怎么投的。 */
  var srcMail = d.source_mail || {};
  var srcDocs = d.resume_documents || [];
  var curDocId = (((d.applications || [])[0] || {}).resume_doc_id
                  || (srcDocs[0] || {}).id || null);
  /* 一份邮件可能同时带简历 + 成绩单/作品集，过去这里只显示第一份，
     “共 2 份”却只有一个“在线打开”，业务方看不到另一份（2026-11-05）。
     现在逐份列出，每份保留自己的打开/下载入口。 */
  var srcFileRows = srcDocs.map(function(doc){
    return '<span class="srcfile">' +
      (String(doc.id) === String(curDocId)
        ? '<span class="srcmeta" style="color:var(--accent);font-weight:600">当前</span>' : '') +
      (doc.received_at
        ? '<span class="srcmeta">' + esc(String(doc.received_at).slice(5, 16)) + '</span>' : '') +
      '<span class="fn" title="' + esc(doc.file_name || '') + '">' +
        esc(doc.file_name || '简历原件') + '</span>' +
      (doc.archived_path
        ? '<button class="btn" onclick="openDoc(' + doc.id + ',1)">在线打开</button>' +
          '<button class="btn" onclick="openDoc(' + doc.id + ',0)">下载</button>'
        : '<span class="srcmeta">原件未留档</span>') +
    '</span>';
  }).join('');
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
    (srcDocs.length
      ? '<span class="srcfiles' + (srcDocs.length > 1 ? ' many' : '') + '">' +
          (srcDocs.length > 1
            ? '<span class="srcmeta srccount">简历原件 · 共 ' + srcDocs.length + ' 份</span>'
            : '') +
          srcFileRows +
        '</span>'
      : '<span class="srcfiles"><span class="srcfile"><span class="srcmeta">无简历原件</span></span></span>') +
  '</div>';

  /* 档案操作条独立于个人信息：横跨右侧详情栏，滚动时固定在右上角。
     2026-10-30 业务方要求重排：
       · 「复制信息 / 推进初筛通过 / 标记已复核」**从这条常驻条上撤下来**，
         挪到姓名那一行的最右边 —— 这三件事都是"看完这个人顺手做一下"，
         挂在顶部常驻条上反而离人最远；
       · 新增绿色「写邮件」，点一下直接跳到写邮件页并带好这个人。 */
  var actionBar = '<div class="detail-toolbar"><div class="dact">' +
    '<button class="btn primary" onclick="saveAll(' + d.id + ',' + (d.application_id || 0) + ')">保存更正</button>' +
    (d.application_id
      ? '<select id="stageSel" class="stagesel" title="推进阶段（写审计）">' +
        (STAGE_CFG.order || []).map(function(k){
          return '<option value="' + esc(k) + '"' + (k === stage ? ' selected' : '') + '>' +
            esc(k) + '</option>';
        }).join('') + '</select>' : '') +
    '<button class="btn" onclick="reanalyze(' + d.id + ')">重新分析</button>' +
    '<button class="btn" onclick="archiveOne(' + d.id + ')">归档</button>' +
  '</div></div>';

  /* 姓名行右侧的动作组（复制信息 / 推进初筛 / 复核 / 写邮件）。
     写邮件排最后且是绿色 —— 它是这条链路上"下一步要做的事"，
     前三项是"顺手记一下"，视觉上不该和它抢。 */
  var headAct = '<div class="dhead-act">' +
    '<button class="btn" onclick="copyBasicInfo()" ' +
      'title="复制姓名 / 性别 / 电话 / 邮箱为纯文本">复制信息</button>' +
    (d.application_id && stage === (STAGE_CFG.default_stage || '新投递')
      ? '<button class="btn" id="btnAdv">推进初筛通过</button>' : '') +
    (d.application_id
      ? '<button class="btn' + (isReviewed ? ' reviewed' : '') + '" id="btnReview" '
        + 'data-undo="' + (isReviewed ? '1' : '0') + '" '
        + 'title="' + (isReviewed ? '已复核 · 点击撤销复核' : '标记这个人的档案已经复核过') + '">'
        + (isReviewed ? '✓ 已复核' : '标记已复核') + '</button>' : '') +
    '<button class="btn btn-green" onclick="mailFor(' + d.id + ')" ' +
      'title="跳到写邮件页，自动带出这个人的收件人、姓名与档案里填过的面试信息">✉ 写邮件</button>' +
  '</div>';

  /* 面试时间的固定候选：按候选人学历生成 30/50 分钟时段，再合并系统字典。
     当前值不在候选里就落到「手动输入」，不会丢掉档案里已有的原文。 */
  var _slotVal = String(d.interview_slot || '').trim();
  /* 拖拽后档案里可能只留下开始时刻；详情里补成完整时段，保存时也一并规范化。 */
  if(/^\d{1,2}:\d{2}$/.test(_slotVal)){
    var _slotRange = schedRangeText(d.interview_date, _slotVal, d.edu_level);
    if(_slotRange) _slotVal = _slotRange;
  }
  var _slotOpts = interviewSlotOptions(d.edu_level);
  var _slotPreset = _slotOpts.indexOf(_slotVal) >= 0;

  /* 操作条与个人信息完全分离：先铺右上操作条，再从下一行开始个人档案。 */
  return actionBar +
    '<div class="dhead profile-head">' +
    '<div class="av-lg">' + esc((d.name || '?').slice(0, 1)) + av + '</div>' +
    '<div class="dmain">' +
    /* 姓名行 = 姓名 + 右侧动作组（2026-10-30）：复制信息贴在姓名最右边，
       推进初筛 / 复核 / 写邮件跟它排在一起。 */
    '<div class="namerow">' +
    '<div class="dname edu-click" title="点击修改姓名" ' +
      'onclick="editField(\'name\', ' + d.id + ')">' +
      esc(d.name || '未识别') + '</div>' +
    headAct +
    '</div>' +
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

    /* v4.3.0：项目经历放到荣誉/论文/专利之前（业务方要求互换位置）。 */
    projHtml +
    /* 荣誉 / 论文 / 专利 + **英语四六级**（业务方 2026-10-21：四六级从头部挪到这里 ——
       它是"资质"，和荣誉/论文/专利是同一类东西；放在姓名下面那排身份信息里显得突兀）。
       标题也跟着改成实情，不再只写三项。 */
    '<div class="sec honosec profile-wide"><h3>荣誉 / 论文 / 专利 / 英语四六级 / 证书</h3>' + honorHtml +
      '<div class="ev cetrow"><b>英语四六级</b>：' +
        edSel('edCetLv', [['', '未识别'], ['六级', '六级'], ['四级', '四级'],
                          ['专业八级', '专业八级'], ['专业四级', '专业四级'],
                          ['考研英语', '考研英语']], d.cet_level, 92) +
      '</div>' +
      certHtml +
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
        '<span class="fld"><span class="fl">面试时间</span>' +
          '<div class="ivslot">' +
            '<select id="ivSlotSel" class="vsel" onchange="ivSlotPicked(this.value)">' +
              '<option value="">固定时段…</option>' +
              _slotOpts.map(function(x){
                return '<option value="' + esc(x) + '"' +
                  (x === _slotVal ? ' selected' : '') + '>' + esc(x) + '</option>';
              }).join('') +
              '<option value="__custom"' +
                ((_slotVal && !_slotPreset) ? ' selected' : '') + '>手动输入…</option>' +
            '</select>' +
            '<input id="ivSlot" placeholder="09:30-10:00" ' +
              'title="支持 09:30-10:00、9点-9点30、上午9:00-9:30；只填开始时间时按学历时长自动补结束" ' +
              'value="' + esc(_slotVal) + '"></div></span>' +
        '<span class="fld"><span class="fl">方式</span>' +
          '<input id="ivMode" list="dlIvMode" placeholder="现场面试" ' +
            'value="' + esc(d.interview_mode || '') + '"></span>' +
        '<datalist id="dlIvMode">' + (IDICT.modes || []).map(function(x){
          return '<option value="' + esc(x) + '"></option>'; }).join('') + '</datalist>' +
        '<span class="fld"><span class="fl">面试地点</span>' +
          '<input id="ivPlace" list="dlIvPlace" placeholder="1519 会议室" ' +
            'value="' + esc(d.interview_place || '') + '"></span>' +
        '<datalist id="dlIvPlace">' + (IDICT.rooms || []).map(function(x){
          return '<option value="' + esc(x) + '"></option>'; }).join('') + '</datalist>' +
        '<span class="fld"><span class="fl">结论</span>' + outcomeField(d) + '</span>' +
      '</div>' +
      /* 录用与入职：第二行，与面试事实之间一条细线 —— 它是"面试之后"的事。 */
      '<div class="ivrow">' +
        /* v4.0.16：录用单位是招聘进度表的归属维度，和“面试单位”分开。
           没有 edit_plan 的角色只读；本机模式/HR 可编辑。
           这里先给免费输入 + datalist，届别单位还没导入时也不挡手输。 */
        '<span class="fld"><span class="fl">录用单位</span>' +
          '<div class="ivslot">' +
            '<select id="obHireUnitSel" class="vsel" onchange="hireUnitPicked(this.value)"' +
              (can('edit_plan') ? '' : ' disabled') + '>' +
              hireUnitOptionsHtml(d.hire_unit || '') + '</select>' +
            '<input id="obHireUnit" list="dlHireUnit" placeholder="如：钛所" value="' +
              esc(d.hire_unit || '') + '"' + (can('edit_plan') ? '' : ' disabled') + '></div></span>' +
        '<datalist id="dlHireUnit">' + (RECRUIT_UNITS || []).map(function(u){
          return '<option value="' + esc(u) + '"></option>'; }).join('') + '</datalist>' +
        '<span class="fld"><span class="fl">签约情况</span>' +
          signStatusField(d) + '</span>' +
        '<span class="fld"><span class="fl">录用函发出</span>' +
          '<input id="obOffer" type="datetime-local" value="' +
            esc(String(d.offer_sent_at || '').replace(' ', 'T').slice(0, 16)) + '"></span>' +
        '<span class="fld"><span class="fl">签约</span>' +
          '<input id="obSigned" type="date" value="' +
            esc(String(d.signed_at || '').slice(0, 10)) + '"></span>' +
        '<span class="fld"><span class="fl">入职期限</span>' +
          '<input id="obDeadline" type="date" value="' +
            esc(String(d.onboard_deadline || '').slice(0, 10)) + '"></span>' +
        /* 人员状态：只记录招聘流程中的入职结果。 */
        '<span class="fld"><span class="fl">人员状态</span>' + onboardStatusField(d) + '</span>' +
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
       v4.0.17：审计只对系统管理员展示；其他角色连区块都不出现。 */
    (can('manage_users')
      ? '<div class="sec profile-wide"><h3>操作记录</h3>' +
        '<div id="auditBox" class="hint">正在读操作记录…</div>' +
        '</div>'
      : '') +

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
  return '<div class="crow dict-row"><label>' + esc(label) + '</label><div class="cinput dictrow">' +
    (list.length ? list.map(function(v, i){
      return '<span class="mchip">' + esc(v) +
        '<b role="button" tabindex="0" aria-label="删除这条" onkeydown="keyClick(event)" ' +
        'onclick="dictDel(\'' + key + '\',' + i + ')" title="删除这条">×</b></span>';
    }).join('') : '<span class="cur none">未设置</span>') +
    '<input class="dict-add" id="dictAdd_' + key + '" ' +
      'placeholder="输入后按回车添加，例如：综合楼 302">' +
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
/* 进度弹窗代际：关窗/开新窗时递增。在途 tick 回来后若代际已变，
   不能再清掉新弹窗的 timer，也不能调用旧阶段的 onDone。 */
var PROG_GEN = 0;
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
  PROG_GEN++;
  if(__progTimer){ clearInterval(__progTimer); __progTimer = null; }
  /* 宽体样式是"这一屏专属"的，关窗必须摘掉，否则下一个普通弹窗也会被撑成 940px */
  var wide = document.querySelector('#mask .dlg.wide');
  if(wide) wide.classList.remove('wide');
  var b = document.getElementById('edDlgBody');
  if(b){ b.style.display = 'none'; b.innerHTML = ''; b.style.whiteSpace = ''; }
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
  var gen = ++PROG_GEN;
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
    if(stopped || gen !== PROG_GEN) return;
    var r = await api('/api/tasks/' + taskId);
    /* await 期间可能已经换到下一阶段了（收取完成 → 接力到逐条分析），
       这时旧轮询必须**立刻退出**，不能再往下走触发旧阶段的 onDone。
       代际校验同时覆盖"用户关窗后又开了新任务"的情况。 */
    if(stopped || gen !== PROG_GEN) return;
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
function ingestAttach(task, opts){
  if(!task || !task.task_id) return;
  var o = opts || {};
  var meta = task.meta || {};
  /* 分析阶段的任务没有 fetch 阶段的 report；meta 里带了上一阶段 report，
     否则刷新后接力会显示"新增 0"（2026-11-13 修）。 */
  var phase = meta.phase || (String(task.title || '').indexOf('分析') >= 0
    ? 'insight' : 'fetch');
  __ingestTask = {taskId: task.task_id, label: '导入', source: null,
                  phase: phase, rep: meta.report || null};
  __ingesting = true;
  setIngestBusy(true);
  /* 页面刷新后的恢复只恢复按钮状态，不强行弹窗 —— 用户可能刚把弹窗关掉。 */
  if(!o.silent) ingestReopen();
}
/* 页面启动时恢复"收取中"按钮状态；不自动弹窗，点按钮再看进度。 */
async function restoreIngestState(){
  try{
    var act = await api('/api/tasks/active');
    if(act && act.ok && act.task) ingestAttach(act.task, {silent: true});
  }catch(e){ /* 查询失败按没有任务处理 */ }
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
  /* 异常/解析失败/无附件/重复跳过：不能只给一个数字，HR 需要知道是哪个人、
     哪份文件、什么原因。details 已由后端逐条带回（live 任务不截断）。 */
  var bad = (rep.details || []).filter(function(d){
    return d && (d.status === 'failed' || d.parse_failed ||
                 d.status === 'no_attachment' || d.status === 'skipped_dup');
  });
  var badHtml = '';
  if(bad.length){
    badHtml = '<div style="margin-top:12px">' +
      '<div style="font-weight:600;color:var(--warn)">需要核对（' + bad.length + '）</div>' +
      '<div style="max-height:240px;overflow:auto;margin-top:6px">' +
      bad.map(function(d){
        var label = d.status === 'failed' ? '异常'
          : (d.parse_failed ? '解析失败（已入库）'
          : (d.status === 'no_attachment' ? '无附件' : '重复跳过'));
        var who = d.name || d.file || '（未识别）';
        var file = d.name && d.file && d.file !== d.name ? '（' + d.file + '）' : '';
        var why = (d.notes || []).join('；');
        return '<div class="ev" style="margin:5px 0">' +
          '<b>' + esc(label) + '</b>：' + esc(who) + esc(file) +
          (why ? '<div class="hint">' + esc(why) + '</div>' : '') +
          '</div>';
      }).join('') +
      '</div></div>';
  }
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
               body: summary + badHtml + stopped + extra + ana});
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
    return /[.](jpe?g|png|bmp|webp|tiff?|pdf|docx?|txt)$/i.test(f.name);
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
      var r = await fetch('/api/upload?name=' + encodeURIComponent(f.name),
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
function todayStamp(){
  var d = new Date();
  var m = String(d.getMonth() + 1), day = String(d.getDate());
  if(m.length < 2) m = '0' + m;
  if(day.length < 2) day = '0' + day;
  return d.getFullYear() + m + day;
}
/* 导出文件名优先用服务端 Content-Disposition，避免所有下载都叫“候选人名单”。 */
function exportFilename(resp, fallback){
  try{
    var cd = resp.headers.get('Content-Disposition') || '';
    var m = cd.match(/filename\*=UTF-8''([^;]+)/i);
    if(m) return decodeURIComponent(m[1]);
    m = cd.match(/filename="?([^";]+)"?/i);
    if(m) return m[1];
  }catch(e){}
  return fallback;
}
/* v4.3.0：导出不再默认全库，先选范围（全部 / 指定阶段 / 待面试的某一天） */
function askExportScope(){
  return new Promise(function(resolve){
    var stages = (STAGE_CFG.order || []);
    document.getElementById('dlgTitle').textContent = '导出名单';
    var editorBody = document.getElementById('edDlgBody');
    if(editorBody){ editorBody.style.display = 'none'; editorBody.innerHTML = ''; }
    var body = document.getElementById('dlgBody');
    body.style.display = '';
    body.innerHTML =
      '<div class="hint" style="margin-bottom:10px">选择要导出的人员范围；' +
        '导出含已归档人员（「是否录用」列需要他们）。</div>' +
      '<label style="display:block;font-size:13px;color:var(--ink-2);margin-bottom:6px">范围' +
        '<select id="expStage" style="display:block;width:100%;height:36px;margin-top:5px;' +
        'border:1px solid var(--line-2);border-radius:var(--r-s);padding:0 10px;font:inherit">' +
          '<option value="">全部候选人</option>' +
          stages.map(function(s){ return '<option value="' + esc(s) + '">' + esc(s) + '</option>'; }).join('') +
        '</select></label>' +
      '<label id="expDateWrap" style="display:none;font-size:13px;color:var(--ink-2);margin-top:10px">' +
        '面试日期（留空 = 全部待面试）' +
        '<input id="expDate" type="date" style="display:block;width:100%;height:36px;margin-top:5px;' +
        'border:1px solid var(--line-2);border-radius:var(--r-s);padding:0 10px;font:inherit"></label>';
    var sel = document.getElementById('expStage');
    var wrap = document.getElementById('expDateWrap');
    sel.onchange = function(){
      wrap.style.display = sel.value === '待面试' ? '' : 'none';
      if(sel.value !== '待面试') document.getElementById('expDate').value = '';
    };
    document.getElementById('dlgNo').textContent = '取消';
    var yes = document.getElementById('dlgYes');
    yes.textContent = '导出'; yes.className = 'btn primary';
    yes.onclick = function(){
      var stage = sel.value || '';
      var date = (document.getElementById('expDate').value || '').trim();
      closeDlg({stage: stage, date: stage === '待面试' ? date : ''});
    };
    document.getElementById('dlgNo').onclick = function(){ closeDlg(null); };
    document.getElementById('mask').onclick = function(ev){
      if(ev.target === ev.currentTarget) closeDlg(null);
    };
    window.__dlgResolve = resolve;
    document.getElementById('mask').classList.add('on');
    sel.focus();
  });
}
async function doExport(){
  var sc = await askExportScope();
  if(!sc) return;
  var p = [];
  if(sc.stage) p.push('stage=' + encodeURIComponent(sc.stage));
  if(sc.date) p.push('interview_date=' + encodeURIComponent(sc.date));
  var label = sc.stage ? (sc.stage + (sc.date ? '（' + sc.date + ' 面试）' : '')) : '全部候选人';
  p.push('scope=' + encodeURIComponent(label + '名单'));
  toast('正在导出名单…');
  var r = await fetch('/api/export/roster?' + p.join('&'), {headers: hdr({})});
  if(!r.ok){ toast('导出失败 HTTP ' + r.status); return; }
  saveBlob(await r.blob(), exportFilename(r, label + '名单-' + todayStamp() + '.xlsx'));
  toast('已导出：' + label);
}
function bundleLastText(last){
  if(!last) return '上次下载：还没有下载记录';
  var f = String(last.date_from || ''), t = String(last.date_to || '');
  var span = (!f && !t) ? '全部初筛通过' : ((f || '最早') + ' 至 ' + (t || '最新'));
  var at = String(last.downloaded_at || '').slice(0, 16);
  var files = (last.files == null) ? '' : (' · ' + last.files + ' 份');
  return '上次下载：' + span + (at ? (' · ' + at) : '') + files;
}
function askBundleRange(last){
  return new Promise(function(resolve){
    document.getElementById('dlgTitle').textContent = '下载初筛简历';
    var editorBody = document.getElementById('edDlgBody');
    if(editorBody){ editorBody.style.display = 'none'; editorBody.innerHTML = ''; }
    document.getElementById('dlgBody').style.display = '';
    document.getElementById('dlgBody').innerHTML =
      '<div class="hint" style="margin-bottom:10px">按<b>投递时间</b>筛选：' +
        '只打包这段时间投递、且当前处于「初筛通过」的简历；' +
        '留空表示不限，也可以只选开始或结束日期。</div>' +
      '<div class="bundle-dates">' +
        '<label>开始日期<input type="date" id="bundleFrom"></label>' +
        '<label>结束日期<input type="date" id="bundleTo"></label>' +
      '</div>' +
      '<div class="bundle-last">' + esc(bundleLastText(last)) + '</div>';
    document.getElementById('dlgNo').textContent = '取消';
    var yes = document.getElementById('dlgYes');
    yes.textContent = '开始下载'; yes.className = 'btn primary';
    function done(v){ closeDlg(v); }
    yes.onclick = function(){
      var f = document.getElementById('bundleFrom').value || '';
      var t = document.getElementById('bundleTo').value || '';
      if(f && t && f > t){
        toast('开始日期不能晚于结束日期');
        return;
      }
      done({date_from:f, date_to:t});
    };
    document.getElementById('dlgNo').onclick = function(){ done(null); };
    document.getElementById('mask').onclick = function(ev){
      if(ev.target === ev.currentTarget) done(null);
    };
    window.__dlgResolve = resolve;
    document.getElementById('mask').classList.add('on');
    var inp = document.getElementById('bundleFrom');
    if(inp) inp.focus();
  });
}
async function doBundle(){
  var lastResp = await api('/api/bundle/initial-pass/last');
  var last = (lastResp && !lastResp.__err) ? lastResp.last : null;
  var range = await askBundleRange(last);
  if(!range) return;
  var qs = [];
  if(range.date_from) qs.push('date_from=' + encodeURIComponent(range.date_from));
  if(range.date_to) qs.push('date_to=' + encodeURIComponent(range.date_to));
  toast('正在打包初筛简历…');
  var r = await fetch('/api/bundle/initial-pass' +
                               (qs.length ? ('?' + qs.join('&')) : ''),
    {method:'POST', headers:hdr({})});
  if(!r.ok){ var j = await r.json().catch(function(){ return {}; });
    toast(j.detail || ('打包失败 HTTP ' + r.status)); return; }
  var span = (range.date_from || '不限') + '至' + (range.date_to || '不限');
  saveBlob(await r.blob(), '初筛通过-' + span + '.zip');
  toast('已打包：' + span);
}
function pageAllSelected(){
  return ITEMS.length > 0 && ITEMS.every(function(x){ return !!SEL[x.id]; });
}
function togglePageSelect(){
  var on = !pageAllSelected();
  setAll(on);
  var chk = document.getElementById('chkAll');
  if(chk) chk.checked = on;
  renderPager();
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
  var ids = Object.keys(SEL).filter(function(k){ return SEL[k]; }).map(Number);
  var n = ids.length;
  /* 勾选**跨筛选/翻页保留**（批量操作需要），但必须让人看见"选中的里有
     当前列表看不见的人"——否则改完筛选点归档，会动到以为已经消失的人。 */
  var visible = {};
  (ITEMS || []).forEach(function(x){ visible[Number(x.id)] = 1; });
  var hidden = ids.filter(function(id){ return !visible[id]; }).length;
  var b = document.getElementById('btnArch');
  b.textContent = '归档勾选（' + n + '）';
  b.disabled = n === 0;
  var clearBtn = document.getElementById('btnClearSel');
  if(clearBtn) clearBtn.hidden = n === 0;
  var hint = document.getElementById('selHint');
  if(hint){
    if(hidden){
      hint.style.display = '';
      hint.textContent = '已选 ' + n + ' 人，其中 ' + hidden + ' 人不在当前筛选结果中';
    }else{
      hint.style.display = 'none';
      hint.textContent = '';
    }
  }
  var ops = document.getElementById('btnBatchOps');
  if(ops){
    ops.textContent = '批量操作（' + n + '） ▾';
    /* 菜单本身始终可开：没勾选时里面两项禁用，HR 能看到有哪些操作 */
    ops.disabled = false;
  }
  var ini = document.getElementById('btnInitial');
  if(ini){
    ini.textContent = '批量初筛通过（' + n + '）';
    ini.disabled = n === 0;
  }
  var ba = document.getElementById('btnBulkAssign');
  if(ba){
    ba.textContent = '批量归岗（' + n + '）';
    ba.disabled = n === 0;
    if(hidden) ba.title = '注意：其中 ' + hidden + ' 人不在当前筛选结果中';
    else ba.removeAttribute('title');
  }
  var c = document.getElementById('btnCompare');
  if(c){
    c.textContent = '对比档案（' + n + '/' + COMPARE_MAX + '）';
    c.disabled = n === 0;
    c.title = n > 5 ? '最多固定 5 个档案，请减少勾选' : '选择 2-5 人后打开固定档案标签';
  }
  /* 分页条上的「全选本页」：逐行勾选后也要同步文案（全选 ↔ 取消全选） */
  var pgs = document.getElementById('pgSelAll');
  if(pgs) pgs.textContent = pageAllSelected() ? '取消全选' : '全选本页';
  var all = document.getElementById('chkAll');
  if(all){
    all.checked = pageAllSelected();
    /* 勾了人但不是"本页全选"时显示半选态，避免看起来"没选中" */
    all.indeterminate = !all.checked && n > 0;
  }
}
/* 清空全部勾选（含跨筛选/翻页保留的那些看不见的人） */
function clearSel(){
  SEL = {};
  Array.prototype.forEach.call(document.querySelectorAll('#list .chk input'),
    function(c){ c.checked = false; });
  var all = document.getElementById('chkAll');
  if(all) all.checked = false;
  syncSel();
  renderPager();
}
async function initialPassChecked(){
  var ids = Object.keys(SEL).filter(function(k){ return SEL[k]; }).map(Number);
  if(!ids.length) return;
  var ok = await askConfirm({
    title: '批量初筛通过',
    okText: '推进',
    body: '把勾选的 ' + ids.length + ' 人推进到「初筛通过」？\n\n' +
          '有系统建议岗位的会先采纳建议并重算；没有建议岗位的仍会推进，' +
          '但保持「待指定」，需要你后续指定岗位。'
  });
  if(!ok) return;
  var r = await api('/api/candidates/bulk-initial-pass', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({ids: ids})});
  if(r.__err){ toast(r.__err, 'danger'); return; }
  toast(r.note || '已处理');
  SEL = {}; syncSel();
  var chk = document.getElementById('chkAll');
  if(chk) chk.checked = false;
  await load();
  if(CUR) pick(CUR);
}

/* v4.3.0：批量归岗 —— 弹窗选择在招岗位，确认后逐人归岗 */
function askJobSelect(jobs){
  return new Promise(function(resolve){
    document.getElementById('dlgTitle').textContent = '批量归岗';
    var editorBody = document.getElementById('edDlgBody');
    if(editorBody){ editorBody.style.display = 'none'; editorBody.innerHTML = ''; }
    var body = document.getElementById('dlgBody');
    body.style.display = '';
    body.innerHTML = '<div class="hint" style="margin-bottom:10px">把勾选的候选人归到哪个岗位？</div>' +
      '<select id="bulkJobSel" style="width:100%;height:36px;border:1px solid var(--line-2);' +
      'border-radius:var(--r-s);padding:0 10px;font:inherit">' +
      jobs.map(function(j){
        return '<option value="' + esc(j.id) + '">' + esc(j.title || ('#' + j.id)) +
          (j.department_name ? '（' + esc(j.department_name) + '）' : '') + '</option>';
      }).join('') + '</select>';
    document.getElementById('dlgNo').textContent = '取消';
    var yes = document.getElementById('dlgYes');
    yes.textContent = '归岗'; yes.className = 'btn primary';
    yes.onclick = function(){
      var el = document.getElementById('bulkJobSel');
      closeDlg(el ? el.value : null);
    };
    document.getElementById('dlgNo').onclick = function(){ closeDlg(null); };
    document.getElementById('mask').onclick = function(ev){
      if(ev.target === ev.currentTarget) closeDlg(null);
    };
    window.__dlgResolve = resolve;
    document.getElementById('mask').classList.add('on');
    var sel = document.getElementById('bulkJobSel');
    if(sel) sel.focus();
  });
}
async function bulkAssignChecked(){
  var ids = Object.keys(SEL).filter(function(k){ return SEL[k]; }).map(Number);
  if(!ids.length) return;
  var jr = await api('/api/jobs');
  if(jr.__err){ toast(jr.__err, 'danger'); return; }
  var jobs = (jr.items || []).filter(function(x){
    return x && x.active !== 0 && x.active !== false;
  });
  if(!jobs.length){ toast('还没有在招岗位，请先到「岗位管理」新建岗位', 'warn'); return; }
  var jobId = await askJobSelect(jobs);
  if(!jobId) return;
  var r = await api('/api/candidates/bulk-assign-job', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids: ids, job_id: Number(jobId)})});
  if(r.__err){ toast(r.__err, 'danger'); return; }
  toast(r.note || '已归岗');
  SEL = {}; syncSel();
  var chk = document.getElementById('chkAll');
  if(chk) chk.checked = false;
  await load();
  if(CUR) pick(CUR);
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
  ['面试与录用','ivPlace','interview_place'],
  ['面试与录用','obOffer','offer_sent_at'], ['面试与录用','obSigned','signed_at'],
  ['面试与录用','obDeadline','onboard_deadline'],
  ['面试与录用','obOnboardStatus','onboard_status'],
  ['面试与录用','obHireUnit','hire_unit'],
  ['面试与录用','obSignStatus','sign_status'],
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
async function openDoc(id, inline){
  /* 先同步占一个窗口再换票：await 之后 window.open 会被弹窗拦截。 */
  var w = inline ? window.open('', '_blank') : null;
  var url = await fileUrl('/api/documents/' + id + '/file' + (inline ? '?inline=1' : ''),
                          'document', {id: Number(id), inline: !!inline});
  if(inline){
    if(w) w.location = url; else window.open(url, '_blank');
  }else{
    var a = document.createElement('a');
    a.href = url; a.download = ''; document.body.appendChild(a); a.click(); a.remove();
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
    /* 先摘再挂，强制重排，动画才会重播（连续两次输错密码时第二次也得抖） */
    e.classList.remove('on');
    if(msg){ void e.offsetWidth; e.classList.add('on'); }
  }
  var u = document.getElementById('loginUser');
  if(u && !u.value){ try{ u.focus(); }catch(_e){} }
  loadLoginHint();
  startLoginNet();
}
/* 失败反馈（v3.2）：框横向轻抖一下。"密码不对"这件事不用读字就该知道；
   连续错两次、三次也照样抖 —— 所以每次先摘类再强制重排。 */
function loginShake(){
  var box = document.querySelector('#loginWrap .loginbox');
  if(!box) return;
  box.classList.remove('shake');
  void box.offsetWidth;
  box.classList.add('shake');
}
/* 首次部署时把默认账号写在登录页上（v3.1.3）。
   原来登录页只有"忘记密码请联系管理员" —— 而管理员自己也不知道初始密码，
   于是双击打开、看到登录框、输不进去，判定为"程序坏了"。
   这条提示只在**密码仍是默认值**时出现，改过之后自动消失。 */
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
/* 密码错误：抖动 + 错误行淡入。**不再用 alert 式的纯文字**——
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
    /* 首次登录强制改密需要旧密码；只在这一轮登录的内存里暂存，不写 localStorage。 */
    window.LOGIN_PASS_TMP = pass;
    try{ await bootAfterLogin(); }
    finally{ window.LOGIN_PASS_TMP = ''; }
    await exited;
    return true;
  }catch(ex){
    setLoginBtn('idle');
    loginFail('网络错误：' + String(ex));
    return false;
  }
}
/* 首次登录强制改密：输入两次新密码 → 调用本人改密接口（带旧密码）→
   用新密码重新登录（改密会清掉该账号全部会话）。 */
async function forcePasswordChange(user, oldPass){
  if(!oldPass) return false;
  toast('首次登录，请先修改初始密码', 'warn');
  var np = await askText('设置新密码', '', '至少 8 位');
  if(!np || np === true) return false;
  if(String(np).length < 8){ toast('密码至少 8 位', 'danger'); return false; }
  var np2 = await askText('再次输入新密码', '', '确认新密码');
  if(!np2 || np2 === true || String(np2) !== String(np)){
    toast('两次输入的密码不一致', 'danger');
    return false;
  }
  var r = await api('/api/users/' + encodeURIComponent(user) + '/password', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({old_password: oldPass, new_password: String(np)})});
  if(r.__err){ toast('修改密码失败：' + r.__err, 'danger'); return false; }
  var lr = await fetch('/api/login', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: user, password: String(np)})});
  var raw = await lr.text(); var lj = {};
  try{ lj = raw ? JSON.parse(raw) : {}; }catch(e){ lj = {}; }
  if(!lr.ok || !lj.token){
    toast('新密码已设置，但自动登录失败，请用新密码重新登录', 'warn');
    return false;
  }
  setToken(lj.token);
  ME = null;
  await loadMe();
  toast('密码已更新');
  return true;
}
/* 登录成功后把界面真正拉起来。**单独一个函数**：因为登录态下这些请求
   第一次都会带 401，必须在拿到 token 之后才发。 */
async function bootAfterLogin(){
  if(AUTH_ENABLED) await loadMe();
  renderWhoami();
  /* 首次登录（或管理员重置后）必须先换掉初始密码，否则业务接口全部 403。
     页面刷新时手里没有旧密码，只能退回登录页重新输入一次。 */
  if(AUTH_ENABLED && ME && ME.must_change_password){
    var _okPw = await forcePasswordChange(ME.username, window.LOGIN_PASS_TMP || '');
    if(!_okPw){
      setToken('');
      ME = null;
      showLogin('请重新登录并修改初始密码');
      return;
    }
  }
  /* renderWhoami() → applyPermissions() 在这里摘掉 body.locked，
     应用区于是从 98.5% 淡入展开 —— 和登录层的淡出是同时的。 */
  applyUrlState();          /* 刷新/收藏进来的筛选条件先落回控件 */
  URL_READY = true;
  enterDefaultView();       /* 默认进入智能助手；hash 有视图时尊重 hash */
  applyPermissions();
  recruitLoadUnits();       /* 提前拿单位列表，档案页“录用单位”下拉才有候选 */
  /* 岗位选项必须先就绪：URL 里的 ?job= 落在 dataset.pending，
     先 load() 会让第一次请求漏掉岗位筛选。 */
  await loadJobFilterOptions();
  await load({push:false});
  await showOverview();
  restoreIngestState();
}
async function recruitLoadUnits(){
  var j = await api('/api/recruit/meta');
  if(j && !j.__err){
    RECRUIT.meta = j;
    RECRUIT_UNITS = j.units || [];
  }
}
async function loadMe(){
  var j = await api('/api/me');
  if(j && !j.__err){ ME = j; }
}
/* 左下角账号区（2026-10-30）。原来这里渲染顶部那条 `.whoami`，
   现在整块搬到侧栏底部，并**放大成一个可点的入口**：
     · 本机模式（没开登录）：显示"本机模式"，菜单里只有系统配置；
     · 登录模式：显示姓名 + 角色，菜单里有账号管理（有权限才显示）与退出登录。 */
function renderWhoami(){
  var nameEl = document.getElementById('acctName');
  var roleEl = document.getElementById('acctRole');
  var avEl = document.getElementById('acctAv');
  var usersBtn = document.getElementById('acctUsers');
  var logoutBtn = document.getElementById('acctLogout');
  if(!nameEl) return;
  if(!AUTH_ENABLED || !ME || !ME.username){
    nameEl.textContent = '本机模式';
    roleEl.textContent = '无需登录 · 数据只在本机';
    avEl.textContent = '本';
    if(usersBtn) usersBtn.hidden = true;
    if(logoutBtn) logoutBtn.hidden = true;
  }else{
    var disp = ME.display_name || ME.username || '';
    nameEl.textContent = disp;
    roleEl.textContent = ME.role_label || ME.role || '';
    avEl.textContent = (disp || '?').slice(0, 1);
    if(logoutBtn) logoutBtn.hidden = false;
  }
  applyPermissions();
}
/* 菜单开合：点外面 / 按 Esc 收起（与「更多筛选」「批量维护」同一套规矩） */
function toggleAcct(ev){
  if(ev && ev.stopPropagation) ev.stopPropagation();
  var menu = document.getElementById('acctMenu');
  var btn = document.getElementById('acctBtn');
  if(!menu) return;
  var on = !menu.classList.contains('on');
  menu.classList.toggle('on', on);
  if(btn) btn.setAttribute('aria-expanded', on ? 'true' : 'false');
}
function closeAcct(){
  var menu = document.getElementById('acctMenu');
  var btn = document.getElementById('acctBtn');
  if(menu) menu.classList.remove('on');
  if(btn) btn.setAttribute('aria-expanded', 'false');
}
function acctGo(view){
  closeAcct();
  if(view === 'users'){ openSettings('users'); return; }
  setView(view);
}
/* 主题：默认跟随系统；在账号菜单里手动切换后记住选择。
   只改 <html data-theme>，颜色全部由 CSS Token 接管，不触碰业务逻辑。 */
function themeIsDark(){
  var t = document.documentElement.getAttribute('data-theme');
  if(t === 'dark') return true;
  if(t === 'light') return false;
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}
function syncThemeBtn(){
  var b = document.getElementById('acctTheme');
  if(b) b.textContent = themeIsDark() ? '浅色模式' : '深色模式';
}
function toggleTheme(){
  var next = themeIsDark() ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try{ localStorage.setItem('tp_theme', next); }catch(e){}
  syncThemeBtn();
}
if(window.matchMedia){
  var _themeMQ = window.matchMedia('(prefers-color-scheme: dark)');
  if(_themeMQ.addEventListener) _themeMQ.addEventListener('change', syncThemeBtn);
}
document.addEventListener('click', function(ev){
  var box = document.getElementById('sfoot');
  if(box && !box.contains(ev.target)) closeAcct();
});
document.addEventListener('keydown', function(ev){
  if(ev.key === 'Escape') closeAcct();
});
/* 按权限显示/隐藏界面元素（v3.0.3）。
   **只负责藏，不负责防**：真正的拦截在服务端 `require()`。
   藏起来是为了让只读用户看不到一堆点了会 403 的按钮。 */
function can(perm){
  if(!AUTH_ENABLED) return true;             /* 本机模式：不限制 */
  return !!(ME && (ME.permissions || []).indexOf(perm) >= 0);
}
function applyPermissions(){
  /* 账号管理只在左下角弹出菜单里（左侧导航已移除，2026-11-02）。
     藏，不是防 —— 服务端仍会 403。 */
  var acctUsers = document.getElementById('acctUsers');
  if(acctUsers) acctUsers.hidden = !(ME && (ME.permissions || []).indexOf('manage_users') >= 0);
  var acctSettings = document.getElementById('acctSettings');
  if(acctSettings) acctSettings.hidden = !can('settings');
  /* 只读角色（没有 set_stage）不该看到任何改动入口 */
  var readonly = AUTH_ENABLED && !can('set_stage');
  document.body.classList.toggle('readonly', readonly);
  /* 系统配置页对没有 settings 权限的人也不该出现 */
  Array.prototype.forEach.call(document.querySelectorAll('.side button[data-view]'), function(b){
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
var LOAD_SEQ = 0;
async function load(opts){
  var seq = ++LOAD_SEQ;
  setListBusy(true);
  var j = await api('/api/candidates' + query());
  /* 快速连续改筛选时，先发的请求可能后返回；只让最后一次渲染，避免
     "列表刚显示新结果又被旧结果盖回去"。 */
  if(seq !== LOAD_SEQ) return;
  setListBusy(false);
  if(j.__err){
    toast(j.__err, 'danger');
    /* 不能静默保留上一批结果：筛选控件已经变了，列表却还是旧数据，
       用户会以为"筛出来就是这些人"。给出持久错误态 + 重试入口。 */
    var lel = document.getElementById('list');
    if(lel) lel.innerHTML = '<div class="empty">列表读取失败：' + esc(j.__err) +
      '<br><button class="btn sm" style="margin-top:8px" onclick="load()">重试</button></div>';
    return;
  }
  ITEMS = j.items || [];
  PAGE = j.paging || {page:1, page_size:0, total:ITEMS.length, total_pages:1};
  FACETS = j;                       /* 筛选器的人数（级联） */
  renderList();
  renderPager();
  renderFacetCounts();
  fillUnitSel(ITEMS);
  syncUrlState(!opts || opts.push !== false);
}
/* 岗位筛选下拉：岗位列表来自 /api/jobs；「未归岗」是特殊值。
   选项是异步加载的，所以保留 URL 里带进来的值（dataset.pending）。 */
function fillJobSel(items){
  var sel = document.getElementById('fJob');
  if(!sel) return;
  var cur = sel.value || sel.dataset.pending || '';
  var jobs = (items || []).filter(function(j){ return j && j.id != null; });
  /* 顺序：不限 → 现有全部岗位 → 未归岗（用户要求未归岗放在最下面） */
  var opts = ['<option value="">岗位：不限</option>'];
  jobs.forEach(function(j){
    opts.push('<option value="' + esc(String(j.id)) + '">' +
      esc(j.title || '未命名') + (j.active === 0 ? '（停用）' : '') + '</option>');
  });
  opts.push('<option value="__unassigned__">未归岗</option>');
  sel.innerHTML = opts.join('');
  var ok = Array.prototype.some.call(sel.options, function(o){ return o.value === cur; });
  sel.value = ok ? cur : '';
  delete sel.dataset.pending;
}
async function loadJobFilterOptions(){
  try{
    var r = await api('/api/jobs');
    fillJobSel(r.items || []);
  }catch(e){ /* 筛选器加载失败不影响人才库主体，保持默认「不限」 */ }
}
/* 面试部门下拉的选项：当前这批人的 `interview_unit` 并集 + 字典里的单位。
   不只靠字典，是因为实际数据里常有字典外的历史写法；每次加载顺手刷新，
   选项才跟着数据走（与 facets 的做法同理）。 */
function fillUnitSel(items){
  var sel = document.getElementById('fUnit');
  if(!sel) return;
  var cur = sel.value, set = {};
  /* 当前选中值必须保留：URL 恢复的部门可能不在本页数据/字典里，
     重建 option 时若丢掉它，筛选条件会静默变成"不限"。 */
  if(cur) set[cur] = null;
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
  var seq = ++DETAIL_SEQ;
  CUR = id;
  /* 窄屏下名单是抽屉：选中人之后自动收起，详情直接占满宽度 */
  if(window.matchMedia && window.matchMedia('(max-width:1100px)').matches){
    document.body.classList.remove('list-open');
  }
  MODE = 'detail';
  COMPARE_IDS = []; COMPARE_DATA = {}; COMPARE_ACTIVE = null;
  highlightRow(id);          /* 只换高亮，不重建列表（见 highlightRow 的说明） */
  var d = await api('/api/candidates/' + id);
  /* 慢网络下先点的 A 可能晚于后点的 B 返回；只让最后一次点击生效。 */
  if(seq !== DETAIL_SEQ || CUR !== id) return;
  if(d.__err){ document.getElementById('detail').innerHTML = '<div class="hint">' + esc(d.__err) + '</div>'; return; }
  CUR_DETAIL = d;
  MODE = 'detail';
  /* 面试字典要先拿到 —— 面试单位/时段/方式的下拉候选都来自它 */
  if(!IDICT_LOADED){
    IDICT_LOADED = true;
    api('/api/interview-dict').then(function(j){
      IDICT = j || {};
      if(seq === DETAIL_SEQ && MODE === 'detail' && CUR === d.id) openDetail(d);
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
['fEdu','fStage','fGender','fUniv','fBachelor','fCet','fDateFrom','fDateTo','fOutcome','fOverseas','fJob','fUnit'].forEach(function(id){
  var el = document.getElementById(id);
  /* 换筛选条件必须回到第 1 页：停在第 2 页再筛，很可能直接看到空列表 */
  if(el) el.addEventListener('change', function(){ PAGE.page = 1; load(); });
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
var HIRE_UNITS_LOADED = false;
var MAIL_TPL = [], MAIL_VARS = {builtin: [], runtime: []}, IDICT = {};
var MAIL_CANDS = [], MAIL_FULL = [];
var MAIL_BOOTED = false, MAIL_CANDS_LOADED = false, MAIL_CANDS_LOADING = false;
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
var VIEWS = ['pool', 'recruit', 'schedule', 'chat', 'mail', 'org', 'archive',
             'sys', 'users'];

/* 日程页状态必须在这里初始化：restoreView 在脚本前部就会按 hash 进入
   initSchedule，而函数定义在脚本后部；var 提升只会把变量变成 undefined。 */
var SCHED = {view:'month', anchor:new Date(), events:[], req:0, booted:false, range:null, hour:46};
var SCHED_DRAG = null;
var SCHED_DRAG_JUST = false;
function setView(v, fromHash){
  /* 系统配置/账号管理不再是主页面：旧 hash 与旧入口统一转到设置弹窗。 */
  if(v === 'sys' || v === 'users'){
    openSettings(v === 'users' ? 'users' : 'model');
    return;
  }
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
             sysView:'sys', chatView:'chat', usersView:'users',
             recruitView:'recruit', scheduleView:'schedule'};
  Object.keys(MAP).forEach(function(id){
    var el = document.getElementById(id);
    if(el) el.classList.toggle('on', MAP[id] === v);
  });
  Array.prototype.forEach.call(document.querySelectorAll('.side button[data-view]'), function(b){
    b.classList.toggle('on', b.dataset.view === v);
  });
  if(v === 'mail') initMail();

  if(v === 'org') initOrg();
  if(v === 'archive') initArch();
  if(v === 'recruit') initRecruit();
  if(v === 'schedule') initSchedule();
  if(v === 'chat') initChat();
}
/* v4.0.17：登录/启动完成后的默认视图。
   没有 hash → 智能助手；有 hash → 尊重刷新前的视图（#recruit 等）。
   ⚠️ setView 会重写 body.className，所以在它之后要再 applyPermissions()
   把 readonly / locked 补回来。 */
function enterDefaultView(){
  var h = (location.hash || '').replace('#', '');
  if(VIEWS.indexOf(h) >= 0){ setView(h, true); return; }
  /* 带筛选参数的收藏/刷新地址没有 hash 时，默认进人才库才看得到筛选效果；
     纯首页仍进智能助手。 */
  if(location.search && location.search.length > 1){ setView('pool', true); return; }
  setView('chat', true);
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
/* ⚠️ 只给**导航项**绑点击（`[data-view]`）。
   2026-11-02 修的 bug：这里原来是 `.side button`，把左下角账号按钮和它的菜单项
   也一起绑上了 —— 那些按钮没有 `data-view`，一点就 `setView(undefined)`，
   body.className 变成 `v-undefined`，整个应用区被隐藏，表现就是
   "点账号管理没反应"（其实页面已被切到一个不存在的视图）。 */
Array.prototype.forEach.call(document.querySelectorAll('.side button[data-view]'), function(b){
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
  /* 重复进入写邮件页不再拉一遍四个接口：DOM 还在，直接复用。
     首次进入先给一个占位，避免用户看着空白等 /api/candidates（827 人时很慢）。 */
  if(MAIL_BOOTED && document.getElementById('mCandInput')){
    loadMailCandidates(true);     /* 页面已在：立即显示，后台刷新候选人 */
    return;
  }
  var mv = document.getElementById('mailView');
  if(mv && !MAIL_BOOTED) mv.innerHTML = '<div class="hint" style="padding:24px">正在打开写邮件…</div>';
  var r = await Promise.all([
    api('/api/mail/templates'), api('/api/interview-dict'), api('/api/mail/smtp')
  ]);
  MAIL_TPL = (r[0].items || []);
  MAIL_VARS = {builtin: r[0].builtin_vars || [], runtime: r[0].runtime_vars || []};
  IDICT = r[1] || {};
  M_SMTP = r[2] || {};
  MAIL_CANDS = [];
  MAIL_FULL = [];
  renderMail();
  MAIL_BOOTED = true;
  setMailSending(M_SENDING);
  loadMailCandidates();      /* 候选人名单后台加载，不挡页面 */
  resumeMailTask();
}
function loadMailCandidates(force){
  if(MAIL_CANDS_LOADING || (MAIL_CANDS_LOADED && !force)) return;
  MAIL_CANDS_LOADING = true;
  api('/api/mail/candidates').then(function(r){
    MAIL_CANDS_LOADING = false;
    if(!r || r.__err) return;
    MAIL_CANDS = (r.items || []).map(function(c){
      return {id: c.id, name: c.name || '', email: c.email || ''};
    });
    MAIL_FULL = MAIL_CANDS.slice();
    MAIL_CANDS_LOADED = true;
    var inp = document.getElementById('mCandInput');
    if(inp && document.activeElement === inp) filterCand();
  }).catch(function(){ MAIL_CANDS_LOADING = false; });
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
  if(!MAIL_CANDS_LOADED && !MAIL_CANDS.length){
    box.innerHTML = '<div class="acItem">正在读取候选人…</div>';
    box.classList.add('on');
    loadMailCandidates();
    return;
  }
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
  var seq = ++CAND_SEQ;
  document.getElementById('mCandDrop').classList.remove('on');
  var d = await api('/api/candidates/' + cid);
  /* 收件人链路同样要防旧响应：慢网络下最终收件人必须是**最后点的那个人**。 */
  if(seq !== CAND_SEQ) return;
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
    '<b role="button" tabindex="0" aria-label="清空收件人" onkeydown="keyClick(event)" ' +
    'onclick="clearRecipient()" title="清空收件人（连同自动带出的信息一起清）">×</b></span>' +
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
    /* 不用原生 <select>：它的下拉列表是浏览器/系统画的（蓝色高亮、方角），
       和页面里其它胶囊输入框完全不是一套皮肤。这里改成应用内自绘的选项列表，
       交互仍是「选中一行 → 确定」，键盘 Tab/Enter 也照常可用。 */
    var picked = '';
    document.getElementById('dlgBody').innerHTML =
      '<div class="picklist" id="dlgPickList" role="listbox">' +
      (ph ? '<button type="button" class="pickrow placeholder" data-v="" role="option">' +
        '<span>' + esc(ph) + '</span><span class="pickcheck">✓</span></button>' : '') +
      (options || []).map(function(o){
        return '<button type="button" class="pickrow" data-v="' + esc(o.value) + '" role="option">' +
          '<span>' + esc(o.label) + '</span><span class="pickcheck">✓</span></button>';
      }).join('') + '</div>';
    document.getElementById('dlgNo').textContent = '取消';
    var yes = document.getElementById('dlgYes');
    yes.textContent = '确定'; yes.className = 'btn primary';
    function done(v){ closeDlg(v || null); }
    var list = document.getElementById('dlgPickList');
    var rows = function(){ return list ? list.querySelectorAll('.pickrow') : []; };
    if(list){
      list.addEventListener('click', function(ev){
        var row = ev.target.closest && ev.target.closest('.pickrow');
        if(!row || !list.contains(row)) return;
        picked = row.dataset.v || '';
        Array.prototype.forEach.call(rows(), function(r){
          r.classList.toggle('on', r === row);
          r.setAttribute('aria-selected', r === row ? 'true' : 'false');
        });
      });
      list.addEventListener('keydown', function(ev){
        var all = Array.prototype.slice.call(rows());
        var i = all.indexOf(document.activeElement);
        if(ev.key === 'ArrowDown' || ev.key === 'ArrowUp'){
          ev.preventDefault();
          var next = ev.key === 'ArrowDown'
            ? all[Math.min(all.length - 1, i + 1)] : all[Math.max(0, i - 1)];
          if(next) next.focus();
        } else if(ev.key === 'Enter' && document.activeElement.classList.contains('pickrow')){
          ev.preventDefault();
          document.activeElement.click();
        }
      });
    }
    yes.onclick = function(){ done(picked || null); };
    document.getElementById('dlgNo').onclick = function(){ done(null); };
    document.getElementById('mask').onclick = function(ev){
      if(ev.target === ev.currentTarget) done(null);
    };
    window.__dlgResolve = resolve;
    document.getElementById('mask').classList.add('on');
    var first = rows()[0];
    setTimeout(function(){ if(first) first.focus(); }, 30);
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

/* v4.0.17：投递进度下方的岗位横向柱。
   数据来自 `/api/jobs` 的 applications_count（只数未归档投递，与岗位页一致）。
   不做点击跳转：人才库的岗位筛选不是这个口径，做成可点会承诺一个不存在的动作。 */
function jobBars(resp){
  var list = ((resp && resp.items) || []).filter(function(j){
    return j && (Number(j.active) !== 0 || (j.applications_count || 0) > 0);
  });
  list.sort(function(a, b){
    return (b.applications_count || 0) - (a.applications_count || 0);
  });
  if(!list.length) return '<div class="hint">还没有岗位投递</div>';
  var max = 1;
  list.forEach(function(j){ max = Math.max(max, j.applications_count || 0); });
  return '<div class="ovjob-list">' + list.map(function(j){
    var n = j.applications_count || 0;
    var pct = Math.round(n / max * 100);
    return '<div class="ovjob"><div class="ovjob-head"><span>' +
      esc(j.title || '未命名岗位') + '</span><b class="num">' + n + '</b></div>' +
      '<div class="ovjob-track"><i style="width:' + pct + '%"></i></div></div>';
  }).join('') + '</div>';
}

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
  /* 图例只列**有人的阶段**：0 人的阶段留在环里没有意义，还会让右侧待办
     看起来比左侧短一截（业务方 2026-11-04）。 */
  var legend = segs.filter(function(x){ return x[1] > 0; }).map(function(x){
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
  fillJobSel(JOBS);
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
          '<div class="rowbar"></div>' +
          '<div style="min-width:0"><div class="nm">' + esc(x.title || '未命名') + '</div>' +
            '<div class="sub">' +
            ((x.department_name || x.dept)
              ? esc(x.department_name || x.dept) + '　· ' : '') +
            '在池 ' + (x.applications_count || 0) + ' 人</div></div>' +
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
  /* 技能/专业改成自由文本 + 多行输入，不再弹规范名候选框；
     保留 loadOntologyHints 供其它页面需要时使用，但这里不挂 datalist。 */
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
  var _edu = String(must.education_min || '');
  var eduSeg = ['', '大专', '本科', '硕士', '博士'].map(function(v){
    return '<button type="button" class="jdseg-btn' + (_edu === v ? ' on' : '') +
      '" data-v="' + esc(v) + '">' + (v || '不限') + '</button>';
  }).join('');
  box.innerHTML =
    '<div class="dhead"><div style="min-width:0">' +
      '<div class="dname">' + esc(J.title || '未命名') + '</div>' +
      '<div class="dmeta">' +
        ((J.department_name || J.dept)
          ? '<span>' + esc(J.department_name || J.dept) + '</span>' : '') +
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
    '<div class="sec"><h3>岗位要求</h3>' +
      '<div class="jdgroup"><div class="jdgrid jdgrid-3">' +
        '<div class="jdfield"><span>最低学历</span>' +
          '<div class="jdseg" id="jdEduSeg">' + eduSeg + '</div>' +
          '<input type="hidden" id="jdEdu" value="' + esc(_edu) + '"></div>' +
        '<label class="jdfield"><span>最低年限</span>' +
          '<input id="jdYears" type="number" min="0" step="1" value="' +
            esc(String(must.years_min == null ? 0 : must.years_min)) + '"></label>' +
        '<label class="jdfield"><span>招聘数量</span>' +
          '<span class="jdinline"><input id="jdHead" type="number" min="0" step="1" value="' +
            esc(String(J.headcount == null ? '' : J.headcount)) + '"><i>人</i></span></label>' +
      '</div></div>' +
      '<div class="jdgroup"><div class="jdgrid">' +
        '<label class="jdfield"><span>必需技能</span>' +
          '<textarea id="jdSkills" rows="2" class="jdlist" ' +
            'placeholder="顿号分隔，如 数据分析、项目管理">' +
            esc((must.skills_required || []).join('、')) + '</textarea></label>' +
        '<label class="jdfield"><span>专业要求</span>' +
          '<textarea id="jdMajors" rows="2" class="jdlist" ' +
            'placeholder="顿号分隔，如 计算机、电子信息">' +
            esc((must.major_required || []).join('、')) + '</textarea></label>' +
        /* 「加分技能」读的是 `preferred.skills`（后端 `_jd_from_req` 的落点）。 */
        '<label class="jdfield"><span>加分技能</span>' +
          '<textarea id="jdPref" rows="2" class="jdlist" ' +
            'placeholder="加分项，有更好，没有不算减分">' +
            esc(((J.jd_json && J.jd_json.preferred && J.jd_json.preferred.skills) || []).join('、')) +
            '</textarea></label>' +
      '</div></div>' +
      '<div class="jdgroup"><label class="jdfield"><span>岗位说明</span>' +
        '<textarea id="jdNote" rows="8" class="jdnote" placeholder="岗位职责、必须说明的事项（自由文本）">' +
          esc(J.jd_json && J.jd_json.note ? J.jd_json.note : '') + '</textarea></label>' +
      '</div>' +
    '</div>' +
    (J.opened_at ? '<div class="sec"><h3>时间</h3><dl class="kv">' +
      '<dt>开放</dt><dd>' + esc(String(J.opened_at).slice(0, 16)) + '</dd>' +
      '<dt>关闭</dt><dd>' + esc(String(J.closed_at || '').slice(0, 16) || '—') + '</dd>' +
      '</dl></div>' : '');
  Array.prototype.forEach.call(box.querySelectorAll('.dact [data-act]'), function(b){
    b.addEventListener('click', function(){ orgAct(b.dataset.act); });
  });
  Array.prototype.forEach.call(box.querySelectorAll('#jdEduSeg .jdseg-btn'), function(b){
    b.addEventListener('click', function(){
      Array.prototype.forEach.call(box.querySelectorAll('#jdEduSeg .jdseg-btn'), function(x){
        x.classList.remove('on');
      });
      b.classList.add('on');
      var hid = document.getElementById('jdEdu');
      if(hid) hid.value = b.dataset.v || '';
    });
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
  if(r.__err){
    /* 失败 ≠ 没有归档档案：保留 ARCH_ITEMS，只显示错误和重试。 */
    var ael = document.getElementById('archList');
    if(ael) ael.innerHTML = '<div class="hint">归档列表读取失败：' + esc(r.__err) +
      '<br><button class="btn sm" style="margin-top:6px" onclick="loadArch()">重试</button></div>';
    return;
  }
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
          : '<img alt="" src="' + esc(x.avatar_url || ('/api/candidates/' + x.id + '/avatar')) +
            '" onerror="this.remove()">';
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
          '<button class="btn sm" data-copy="' + esc(d.phone) + '">复制</button></span>' : '') +
      (d.email && d.email !== '—'
        ? '<span class="cval1">' + esc(d.email) +
          '<button class="btn sm" data-copy="' + esc(d.email) + '">复制</button></span>' : '') +
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
    var rr = await fetch('/api/sources/bundle',
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
/* 设置弹窗复用原来的系统配置渲染；页面视图仍保留 sysView 以兼容旧入口。 */
var SYS_TARGET = 'sysView';
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
var USERS_TARGET = 'usersView';

async function initUsers(target){
  if(target) USERS_TARGET = target;
  var box = document.getElementById(USERS_TARGET);
  if(!box) box = document.getElementById('usersView');
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

function renderUsers(target){
  if(target) USERS_TARGET = target;
  var box = document.getElementById(USERS_TARGET) || document.getElementById('usersView');
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
      '<td><select class="sel" data-user-role="' + esc(u.username) + '">' +
        roles.map(function(x){
          return '<option value="' + esc(x.key) + '"' +
            (x.key === u.role ? ' selected' : '') + '>' + esc(x.label) + '</option>';
        }).join('') + '</select></td>' +
      '<td class="usub">' + esc(r.desc || '') + '</td>' +
      '<td style="white-space:nowrap">' +
        '<button class="btn" data-user-reset="' + esc(u.username) + '">重置密码</button> ' +
        (u.active
          ? (isMe ? '' : '<button class="btn btn-danger" data-user-active="' +
              esc(u.username) + '" data-active="0">停用</button>')
          : '<button class="btn" data-user-active="' + esc(u.username) +
              '" data-active="1">启用</button>') +
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
      /* 权限矩阵：表头用**竖排**（每个权限两字一行），14 列才排得下。
         横排会被挤成"查看查看使用让助…"这种没法读的东西。 */
      '<table class="utable pmtable"><thead><tr><th style="width:190px">角色</th><td>' +
        Object.keys(USERS.perm_labels || {}).map(function(p){
          var lab = USERS.perm_labels[p];
          return '<span class="pmcol" title="' + esc(lab) + '">' +
            esc(lab.slice(0, 2)) + '</span>';
        }).join('') + '</td></tr></thead><tbody>' + matrix + '</tbody></table>' +
      '<div class="hint" style="margin-top:16px">密码至少 ' + (USERS.min_password_len || 8) +
        ' 位。修改角色或停用账号会**立即清除该账号的登录状态**。</div>' +
    '</div>';
  /* 账号名来自库里的用户输入：用 data-* + 事件监听，不拼进 inline onclick
     （HTML 转义不能代替 JS 字符串转义）。 */
  Array.prototype.forEach.call(box.querySelectorAll('[data-user-role]'), function(sel){
    sel.addEventListener('change', function(){ userSetRole(sel.dataset.userRole, sel.value); });
  });
  Array.prototype.forEach.call(box.querySelectorAll('[data-user-reset]'), function(b){
    b.addEventListener('click', function(){ userResetPassword(b.dataset.userReset); });
  });
  Array.prototype.forEach.call(box.querySelectorAll('[data-user-active]'), function(b){
    b.addEventListener('click', function(){
      userSetActive(b.dataset.userActive, b.dataset.active === '1');
    });
  });
}

/* ============================================================
   招聘进度（v4.0.16）
   页面、导出、智能助手都读同一套后端口径；这里只做展示与交互。
   三条展示原则：
     · 计划数可编辑，但必须点“保存计划”才落库（不会边输入边改库）；
     · 签约人只读，点名字跳档案；归属在“数据体检”里改；
     · 数据不齐的人不会“消失”，会出现在体检里。
   ============================================================ */
var RECRUIT = {meta:null, data:null, roster:null, yearId:null, tab:'progress',
               edit:false, expanded:{}, filters:{unit:'', edu:'', onlyGap:false},
               rosterFilters:{date_from:'', date_to:'', unit:'', edu:'',
                              outcome:'', keyword:''},
               pendingFile:null};
var RECRUIT_UNITS = [];

function rpPct(v){
  return (v === null || v === undefined) ? '—' : (Number(v) * 100).toFixed(1) + '%';
}
function rpNum(v){
  return (v === null || v === undefined || v === '') ? 0 : Number(v);
}

async function initRecruit(){
  var box = document.getElementById('recruitView');
  if(!box) return;
  box.innerHTML = '<div class="hint" style="padding:20px">读取招聘进度…</div>';
  var meta = await api('/api/recruit/meta');
  if(meta.__err){
    box.innerHTML = '<div class="rp-empty">' + esc(meta.__err) + '</div>';
    return;
  }
  RECRUIT.meta = meta;
  RECRUIT_UNITS = meta.units || [];
  if(!RECRUIT.yearId){
    RECRUIT.yearId = meta.default_year_id
      || (meta.years && meta.years[0] && meta.years[0].id) || null;
  }
  if(!RECRUIT.yearId){
    RECRUIT.data = null; RECRUIT.roster = null;
    renderRecruitPage();
    return;
  }
  await recruitLoad();
}

async function recruitLoad(){
  var d = await api('/api/recruit/progress?year_id=' + encodeURIComponent(RECRUIT.yearId));
  if(d.__err){ toast('读取进度失败：' + d.__err, 'danger'); return; }
  RECRUIT.data = d;
  RECRUIT.roster = null;
  /* 单位列表可能因新增/删除单位变化，档案页的录用单位 datalist 跟着刷新 */
  if(d && d.units){
    RECRUIT_UNITS = d.units;
    if(RECRUIT.meta) RECRUIT.meta.units = d.units;
  }
  if(RECRUIT.tab === 'roster') await recruitLoadRoster();
  renderRecruitPage();
}

async function recruitPickYear(id){
  RECRUIT.yearId = Number(id);
  RECRUIT.edit = false;
  await recruitLoad();
}

async function recruitTab(tab){
  RECRUIT.tab = tab;
  if(tab === 'roster' && !RECRUIT.roster) await recruitLoadRoster();
  renderRecruitPage();
}

async function recruitLoadRoster(){
  var f = RECRUIT.rosterFilters || {};
  var q = '/api/recruit/roster?year_id=' + encodeURIComponent(RECRUIT.yearId);
  Object.keys(f).forEach(function(k){
    if(f[k]) q += '&' + k + '=' + encodeURIComponent(f[k]);
  });
  var d = await api(q);
  if(d.__err){ toast('读取面试名单失败：' + d.__err, 'danger'); return; }
  RECRUIT.roster = d;
}

function rpCard(k, v, s, tone){
  return '<div class="rp-card c-' + (tone || 'blue') + '"><div class="k">' +
    esc(k) + '</div><div class="v">' + esc(v) +
    (s ? '<span class="s">' + esc(s) + '</span>' : '') + '</div></div>';
}
function rpTab(id, label){
  return '<button class="rp-tab' + (RECRUIT.tab === id ? ' on' : '') +
    '" onclick="recruitTab(\'' + id + '\')">' + esc(label) + '</button>';
}

function rhKpi(label, value, tone){
  return '<div class="rh-kpi"><span class="rh-k">' + esc(label) + '</span>' +
    '<b class="rh-v ' + tone + '">' + esc(value) + '</b></div>';
}

/* 招聘进度主视觉：一个完成率环 + 四个关键数 + 学历分布条。
   替换原来 4 张 KPI 卡 + 4 张学历卡（8 个同构卡片）；这里只保留一个
   视觉锚点（环），其余用紧凑的数字与进度条，颜色只表达状态。 */
function recruitHero(t, byEdu){
  var should = rpNum(t.should_hire), signed = rpNum(t.signed_count), gap = rpNum(t.gap);
  var rate = t.rate;
  var ringPct = (rate === null || rate === undefined)
    ? 0 : Math.max(0, Math.min(1, Number(rate)));
  var ringColor = (rate === null || rate === undefined)
    ? 'var(--line-2)' : (rate >= 1 ? 'var(--ok)' : (rate > 0 ? 'var(--accent)' : 'var(--line-2)'));
  var ringText = (rate === null || rate === undefined)
    ? 'var(--ink-3)' : (rate >= 1 ? 'var(--ok)' : (rate > 0 ? 'var(--accent-ink)' : 'var(--ink-3)'));
  var C = 2 * Math.PI * 48;
  var dash = (C * ringPct).toFixed(1), rest = (C - C * ringPct).toFixed(1);
  var ring = '<div class="rh-ring">' +
    '<svg viewBox="0 0 120 120" role="img" aria-label="招聘完成率">' +
      '<circle class="rh-ring-track" cx="60" cy="60" r="48" fill="none" stroke-width="12"/>' +
      '<circle cx="60" cy="60" r="48" fill="none" stroke="' + ringColor + '" ' +
        'stroke-width="12" stroke-linecap="round" stroke-dasharray="' + dash + ' ' + rest + '" ' +
        'transform="rotate(-90 60 60)"/>' +
    '</svg>' +
    '<div class="rh-ring-center"><b style="color:' + ringText + '">' + rpPct(rate) + '</b>' +
      '<span>已签 ' + signed + ' / 应招 ' + should + '</span></div></div>';
  var kpis = '<div class="rh-kpis">' +
    rhKpi('应招总数', should, 'blue') +
    rhKpi('已签人数', signed, 'green') +
    rhKpi('签约缺口', gap, gap < 0 ? 'red' : 'amber') +
    rhKpi('完成率', rpPct(rate), 'purple') + '</div>';
  var degrees = (byEdu || []).map(function(x){
    var n = rpNum(x.should_hire), sg = rpNum(x.signed_count), gp = rpNum(x.gap);
    var tone = {'博士':'doctor', '硕士':'master', '本科':'bachelor', '大专':'college'}[x.edu_level] || '';
    var gcls = gp > 0 ? 'pos' : (gp < 0 ? 'neg' : 'zero');
    return '<div class="rh-degree"><div class="rh-dhead">' +
      '<span class="rh-dot ' + tone + '"></span>' +
      '<span class="rh-dname">' + esc(x.edu_level) + '</span></div>' +
      '<div class="rh-dstat">应招 <b>' + n + '</b></div>' +
      '<div class="rh-dstat">已签 <b>' + sg + '</b></div>' +
      '<div class="rh-dstat gap ' + gcls + '">缺口 <b>' + gp + '</b></div></div>';
  }).join('');
  return '<section class="recruit-hero"><div class="rh-left">' + ring + '</div>' +
    '<div class="rh-right">' + kpis + '<div class="rh-degrees">' + degrees + '</div></div></section>';
}

function renderRecruitPage(){
  var box = document.getElementById('recruitView');
  if(!box) return;
  var meta = RECRUIT.meta || {years:[], can_edit:false, can_export:false};
  var d = RECRUIT.data;
  var year = d && d.year;
  var h = '<div class="rp-head"><h2>招聘进度</h2>';
  if(year){
    h += '<span class="hint">' + esc(year.period_start) + ' 至 ' +
         esc(year.period_end) + '</span>';
  }
  h += '<div class="rp-toolbar">';
  if(meta.years && meta.years.length){
    h += '<select class="sel" onchange="recruitPickYear(this.value)">' +
      meta.years.map(function(y){
        return '<option value="' + y.id + '"' +
          (String(y.id) === String(RECRUIT.yearId) ? ' selected' : '') + '>' +
          esc(y.label) + '（' + esc(y.period_start) + '~' + esc(y.period_end) + '）' +
          '</option>';
      }).join('') + '</select>';
  }
  if(meta.can_edit) h += '<button class="btn" onclick="recruitNewYear()">新建届别</button>';
  if(meta.can_edit) h += '<button class="btn primary" onclick="recruitImport()">导入计划</button>';
  if(meta.can_export){
    h += '<select class="sel" id="rpExportKind">' +
      '<option value="official">官方版式</option>' +
      '<option value="flat">平铺明细</option>' +
      '<option value="roster">面试总名单</option></select>' +
      '<button class="btn btn-green" onclick="recruitExport()">导出</button>';
  }
  h += '<button class="btn" onclick="initRecruit()">刷新</button>';
  if(can('chat')){
    h += '<button class="btn" onclick="recruitAskAssistant()">让助手总结</button>';
  }
  h += '</div></div>';

  if(!year){
    h += '<div class="rp-empty"><h3 style="margin:0 0 8px">还没有招聘届别</h3>' +
      '<div>导入现有的《2027届招聘进度表》，或先建一个空白届别。</div>' +
      '<div style="margin-top:14px">' +
      (meta.can_edit ? '<button class="btn primary" onclick="recruitImport()">导入 2027届招聘进度表</button> ' +
        '<button class="btn" onclick="recruitNewYear()">新建空白届别</button>' : '') +
      '</div></div>';
    box.innerHTML = h;
    return;
  }

  var t = (d && d.totals) || {};
  h += recruitHero(t, d.by_education || []);
  var hc = (d.health && d.health.count) || 0;
  h += '<div class="rp-tabs">' + rpTab('progress', '进度总表') +
    rpTab('roster', '面试总名单') +
    rpTab('health', '数据体检' + (hc ? '（' + hc + '）' : '')) +
    '</div><div id="rpBody"></div>';
  box.innerHTML = h;
  renderRecruitTab();
}

function renderRecruitTab(){
  var body = document.getElementById('rpBody');
  if(!body) return;
  if(!RECRUIT.data || !RECRUIT.data.year){ body.innerHTML = ''; return; }
  if(RECRUIT.tab === 'roster'){ renderRecruitRoster(body); return; }
  if(RECRUIT.tab === 'health'){ renderRecruitHealth(body); return; }
  renderRecruitProgress(body);
}

function recruitFilter(k, v){
  RECRUIT.filters[k] = v;
  renderRecruitTab();
}
function recruitToggleEdit(on){
  RECRUIT.edit = !!on;
  renderRecruitTab();
}

function rpInput(r, field){
  var labels = {plan_approved:'上会审批', plan_extra:'追加',
                plan_carryover:'转上年度', plan_cancel:'应取消'};
  return '<td><input class="rp-num" id="rp-' + field + '-' + r.plan_id +
    '" data-plan="' + r.plan_id + '" data-field="' + field +
    '" type="number" min="0" value="' + rpNum(r[field]) + '" title="' +
    labels[field] + '" oninput="recruitRecalc(' + r.plan_id + ')"></td>';
}

/* 横向滚动时前三列固定：left 偏移必须按**实际列宽**算，
   写死 52/148 会因为单元格内边距而错位，出现固定列之间的缝隙。 */
function recruitFreezeCols(){
  var t = document.querySelector('#recruitView .rp-table');
  if(!t) return;
  var ths = t.querySelectorAll('thead th');
  if(ths.length < 3) return;
  var w1 = ths[0].getBoundingClientRect().width;
  var w2 = ths[1].getBoundingClientRect().width;
  t.style.setProperty('--freeze1', w1 + 'px');
  t.style.setProperty('--freeze2', (w1 + w2) + 'px');
}
window.addEventListener('resize', recruitFreezeCols);
function renderRecruitProgress(body){
  var d = RECRUIT.data || {};
  var rows = (d.rows || []).slice();
  var f = RECRUIT.filters || {};
  if(f.unit) rows = rows.filter(function(r){ return r.unit_name === f.unit; });
  if(f.edu) rows = rows.filter(function(r){ return r.edu_level === f.edu; });
  if(f.onlyGap) rows = rows.filter(function(r){ return rpNum(r.gap) > 0; });

  var h = '<div class="rp-bar">' +
    '<select class="sel" onchange="recruitFilter(\'unit\',this.value)">' +
      '<option value="">单位：全部</option>' +
      (d.units || []).map(function(u){
        return '<option' + (f.unit === u ? ' selected' : '') + '>' + esc(u) + '</option>';
      }).join('') + '</select>' +
    '<select class="sel" onchange="recruitFilter(\'edu\',this.value)">' +
      '<option value="">学历：全部</option>' +
      ['博士','硕士','本科','大专'].map(function(e){
        return '<option' + (f.edu === e ? ' selected' : '') + '>' + esc(e) + '</option>';
      }).join('') + '</select>' +
    '<label class="allbox"><input type="checkbox" ' + (f.onlyGap ? 'checked' : '') +
      ' onchange="recruitFilter(\'onlyGap\',this.checked)"> 只看有缺口</label>';
  if(RECRUIT.meta && RECRUIT.meta.can_edit){
    h += RECRUIT.edit
      ? '<button class="btn primary" onclick="recruitSavePlans()">保存计划</button>' +
        '<button class="btn" onclick="recruitToggleEdit(false)">取消</button>'
      : '<button class="btn" onclick="recruitToggleEdit(true)">编辑计划</button>';
    h += '<button class="btn" onclick="recruitAddUnit()">新增单位</button>';
  }
  h += '</div>';

  if(!rows.length){
    body.innerHTML = h + '<div class="rp-empty">当前筛选下没有计划行。' +
      (d.needs_plan ? '请先“导入计划”。' : '') + '</div>';
    return;
  }

  var groups = [], map = {};
  rows.forEach(function(r){
    var k = r.unit_key || r.unit_name;
    if(!map[k]){ map[k] = []; groups.push({unit:r.unit_name, rows:map[k]}); }
    map[k].push(r);
  });

  var head = '<tr><th class="sticky-seq" style="width:52px">序号</th>' +
    '<th class="sticky-unit" style="width:96px">单位</th>' +
    '<th class="sticky-edu" style="width:64px">学历</th>' +
    '<th class="plan">上会审批</th><th class="plan">追加</th>' +
    '<th class="plan">转上年度</th><th class="plan">应取消</th>' +
    '<th class="calc">应招总数</th><th class="calc">已签</th>' +
    '<th class="calc">签约缺口</th><th class="calc">完成率</th>' +
    '<th class="sign" style="min-width:420px">签约人</th></tr>';
  var bodyRows = '', seq = 0;
  groups.forEach(function(g){
    seq++;
    var byEdu = {};
    g.rows.forEach(function(r){ byEdu[r.edu_level] = r; });
    var list = ['博士','硕士','本科','大专'].map(function(e){ return byEdu[e]; })
      .filter(Boolean);
    if(!list.length) return;
    list.forEach(function(r, idx){
      var trCls = (idx === 0 ? 'unit-start ' : '') +
                  (rpNum(r.signed_count) > 0 ? 'has-sign ' : '') +
                  (seq % 2 ? 'unit-odd' : 'unit-even');
      bodyRows += '<tr class="' + trCls + '">';
      if(idx === 0){
        bodyRows += '<td rowspan="' + list.length + '" class="num sticky-seq">' + seq + '</td>' +
          '<td rowspan="' + list.length + '" class="unit sticky-unit">' + esc(g.unit) +
          (RECRUIT.edit
            ? ' <button class="rp-unit-del" title="删除单位" onclick="recruitDeleteUnit(' +
              jq(g.unit) + ')">×</button>'
            : '') +
          '</td>';
      }
      bodyRows += '<td class="sticky-edu">' + esc(r.edu_level) + '</td>';
      if(RECRUIT.edit && r.plan_id){
        bodyRows += rpInput(r, 'plan_approved') + rpInput(r, 'plan_extra') +
          rpInput(r, 'plan_carryover') + rpInput(r, 'plan_cancel');
      }else{
        bodyRows += '<td class="num">' + rpNum(r.plan_approved) + '</td>' +
          '<td class="num">' + rpNum(r.plan_extra) + '</td>' +
          '<td class="num">' + rpNum(r.plan_carryover) + '</td>' +
          '<td class="num">' + rpNum(r.plan_cancel) + '</td>';
      }
      var rate = r.rate;
      var rateCls = (rate === null || rate === undefined) ? 'rate-none'
                  : (rate >= 1 ? 'rate-good' : (rate > 0 ? 'rate-mid' : 'rate-zero'));
      bodyRows += '<td class="num" id="rp-should-' + r.plan_id + '">' +
          rpNum(r.should_hire) + '</td>' +
        '<td class="num ' + (rpNum(r.signed_count) > 0 ? 'signed-pos' : '') +
          '" id="rp-signed-' + r.plan_id + '" data-v="' +
          rpNum(r.signed_count) + '">' + rpNum(r.signed_count) + '</td>' +
        '<td class="num ' + (rpNum(r.gap) < 0 ? 'gap-neg' : (rpNum(r.gap) > 0 ? 'gap-pos' : '')) +
          '" id="rp-gap-' + r.plan_id + '">' + rpNum(r.gap) + '</td>' +
        '<td class="num ' + rateCls + '" id="rp-rate-' + r.plan_id + '">' + rpPct(r.rate) + '</td>' +
        '<td class="signee-cell">' + (function(){
          var ss = r.signees || [];
          if(!ss.length) return '<span class="hint">—</span>';
          return ss.map(function(s){
            var dl = s.onboard_deadline ? String(s.onboard_deadline).slice(0, 10) : '未填';
            return '<span class="rp-signee" title="签约：' + esc(s.signed_at || '—') +
              '；入职期限：' + esc(dl) +
              '" onclick="recruitOpenCandidate(' + s.candidate_id + ')">' +
              esc(s.name) + '<i class="rp-deadline">（' + esc(dl) + '）</i></span>';
          }).join('');
        })() + '</td></tr>';
    });
  });
  body.innerHTML = h + '<div class="rp-wrap"><table class="rp-table"><thead>' +
    head + '</thead><tbody>' + bodyRows + '</tbody></table></div>';
  recruitFreezeCols();
}

function recruitRecalc(planId){
  var get = function(field){
    var el = document.getElementById('rp-' + field + '-' + planId);
    return el ? (parseInt(el.value || '0', 10) || 0) : 0;
  };
  var should = get('plan_approved') + get('plan_extra') +
               get('plan_carryover') - get('plan_cancel');
  var signedEl = document.getElementById('rp-signed-' + planId);
  var signed = signedEl ? (parseInt(signedEl.getAttribute('data-v') || '0', 10) || 0) : 0;
  var gap = should - signed;
  var rate = should ? signed / should : null;
  var s = document.getElementById('rp-should-' + planId);
  if(s) s.textContent = should;
  var g = document.getElementById('rp-gap-' + planId);
  if(g){
    g.textContent = gap;
    g.className = 'num ' + (gap < 0 ? 'gap-neg' : (gap > 0 ? 'gap-pos' : ''));
  }
  var rt = document.getElementById('rp-rate-' + planId);
  if(rt){
    rt.textContent = rpPct(rate);
    rt.className = 'num ' + (rate === null ? 'rate-none'
      : (rate >= 1 ? 'rate-good' : (rate > 0 ? 'rate-mid' : 'rate-zero')));
  }
}

async function recruitDeleteUnit(name){
  name = String(name || '').trim();
  if(!name) return;
  var ok = await askConfirm({
    title: '删除单位', danger: true, okText: '删除',
    body: '从本届计划删除「' + name + '」的全部学历行？\n\n' +
          '已签约人员不会被删除，但会进入「单位不在计划」，需要重新归属。'
  });
  if(!ok) return;
  var r = await api('/api/recruit/plans/delete-unit', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({year_id:RECRUIT.yearId, unit_name:name})
  });
  if(r.__err){ toast('删除失败：' + r.__err, 'danger'); return; }
  toast(r.note || ('已删除单位：' + name), r.signed_count ? 'warn' : 'ok');
  await recruitLoad();
}

async function recruitAddUnit(){
  var name = await askText('新增单位', '', '如：钛所');
  if(!name || name === true) return;
  var r = await api('/api/recruit/plans', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({year_id:RECRUIT.yearId, unit_name:String(name).trim()})
  });
  if(r.__err){ toast('新增失败：' + r.__err, 'danger'); return; }
  toast('已新增单位：' + name);
  await recruitLoad();
}

async function recruitSavePlans(){
  var rows = {};
  Array.prototype.forEach.call(document.querySelectorAll('.rp-num'), function(el){
    var pid = el.getAttribute('data-plan');
    if(!pid) return;
    if(!rows[pid]) rows[pid] = {id:Number(pid)};
    rows[pid][el.getAttribute('data-field')] = parseInt(el.value || '0', 10) || 0;
  });
  var r = await api('/api/recruit/plans/batch', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({year_id:RECRUIT.yearId,
      rows:Object.keys(rows).map(function(k){ return rows[k]; })})
  });
  if(r.__err){ toast('保存失败：' + r.__err, 'danger'); return; }
  toast(r.note || '计划已保存');
  RECRUIT.edit = false;
  await recruitLoad();
}

function renderRecruitRoster(body){
  var r = RECRUIT.roster;
  var f = RECRUIT.rosterFilters || {};
  var units = (RECRUIT.data && RECRUIT.data.units) || [];
  var h = '<div class="rp-bar">' +
    '<input id="rpFrom" type="text" class="sel" placeholder="开始日期" value="' +
      esc(f.date_from || '') + '" onfocus="this.type=\'date\'" ' +
      'onblur="if(!this.value)this.type=\'text\'">' +
    '<span class="hint">~</span>' +
    '<input id="rpTo" type="text" class="sel" placeholder="结束日期" value="' +
      esc(f.date_to || '') + '" onfocus="this.type=\'date\'" ' +
      'onblur="if(!this.value)this.type=\'text\'">' +
    '<select class="sel" id="rpRUnit"><option value="">单位：全部</option>' +
      units.map(function(u){
        return '<option' + (f.unit === u ? ' selected' : '') + '>' + esc(u) + '</option>';
      }).join('') + '</select>' +
    '<select class="sel" id="rpREdu"><option value="">学历：全部</option>' +
      ['博士','硕士','本科','大专'].map(function(e){
        return '<option' + (f.edu === e ? ' selected' : '') + '>' + esc(e) + '</option>';
      }).join('') + '</select>' +
    '<select class="sel" id="rpROutcome"><option value="">结论：全部</option>' +
      ['拟录用','不录用','未签约','已签约','拒签','毁约','未判'].map(function(o){
        return '<option' + (f.outcome === o ? ' selected' : '') + '>' + esc(o) + '</option>';
      }).join('') + '</select>' +
    '<input id="rpKw" class="search" placeholder="姓名/学校/单位" value="' +
      esc(f.keyword || '') + '">' +
    '<button class="btn primary" onclick="recruitRosterSearch()">查询</button>' +
    '<button class="btn" onclick="recruitRosterReset()">重置</button></div>';
  if(!r){ body.innerHTML = h + '<div class="rp-empty">读取面试名单…</div>'; return; }
  if(!r.items || !r.items.length){
    body.innerHTML = h + '<div class="rp-empty">本届还没有面试/签约记录。</div>';
    return;
  }
  var heads = ['序号','面试日期','单位','姓名','学校','学历','面试方式','电话',
               '是否录用','录用函','发放时间','签约情况','签约时间','毕业时间',
               '入职情况','备注'];
  var trs = r.items.map(function(x){
    var vals = [x.seq, x.interview_date, x.unit, x.name, x.school, x.edu_level,
                x.interview_mode, x.phone, x.verdict, x.offer_sent,
                x.offer_sent_at, x.sign_status, x.signed_at, x.grad_date,
                x.onboard_status, x.note];
    return '<tr>' + vals.map(function(v, i){
      return '<td' + (i === 15 ? ' style="text-align:left;min-width:160px"' : '') +
        '>' + esc(v || '—') + '</td>';
    }).join('') + '</tr>';
  }).join('');
  body.innerHTML = h + '<div class="rp-wrap"><table class="rp-table"><thead><tr>' +
    heads.map(function(x){ return '<th>' + esc(x) + '</th>'; }).join('') +
    '</tr></thead><tbody>' + trs + '</tbody></table></div>';
}

async function recruitRosterSearch(){
  var v = function(id){
    var el = document.getElementById(id);
    return el ? String(el.value || '').trim() : '';
  };
  RECRUIT.rosterFilters = {
    date_from:v('rpFrom'), date_to:v('rpTo'), unit:v('rpRUnit'),
    edu:v('rpREdu'), outcome:v('rpROutcome'), keyword:v('rpKw')
  };
  RECRUIT.roster = null;
  await recruitLoadRoster();
  renderRecruitTab();
}

async function recruitRosterReset(){
  RECRUIT.rosterFilters = {date_from:'', date_to:'', unit:'', edu:'',
                           outcome:'', keyword:''};
  RECRUIT.roster = null;
  await recruitLoadRoster();
  renderRecruitTab();
}

function renderRecruitHealth(body){
  var d = RECRUIT.data || {};
  var h = d.health || {};
  var defs = [
    ['missing_signed_at','待补签约时间','已签约但没填签约时间，未计入'],
    ['invalid_signed_at','签约时间无效','格式无法识别，未计入'],
    ['signed_without_conclusion','已签约但结论未判','签约情况为已签约，但面试结论不是拟录用'],
    ['out_of_period','期外签约','签约时间不在本届'],
    ['stage_mismatch','结论与阶段不一致','已签约但阶段不是已发放offer'],
    ['missing_hire_unit','待归属录用单位','缺少录用单位，未计入'],
    ['unknown_edu','学历异常','不在博士/硕士/本科/大专四档'],
    ['unit_not_in_plan','单位不在计划','录用单位不在本届计划'],
    ['conflicting_signings','签约冲突','同一人多条已签约且归属不一致，全部未计入'],
    ['duplicate_signed','重复签约','同单位同学历多条，已按 1 人计'],
    ['signed_without_plan','无计划签约','计划为 0 但有签约'],
    ['many_signees','签约人超过 10 列','导出会自动扩展列'],
    ['duplicate_names','重名签约人','同一计划行有重名，请核对']
  ];
  var cards = defs.map(function(d0){
    var b = h[d0[0]] || {count:0, items:[]};
    if(!b.count) return '';
    var items = (b.items || []).map(function(it){
      var meta = [it.unit, it.edu_level, it.signed_at].filter(Boolean).join(' · ');
      var actions = '';
      if(it.application_id && d0[0] === 'missing_signed_at'){
        actions += '<button class="btn sm" onclick="recruitQuickFixDate(' +
          it.application_id + ')">补时间</button>';
      }
      if(it.application_id && (d0[0] === 'missing_hire_unit' ||
          d0[0] === 'unit_not_in_plan' || d0[0] === 'conflicting_signings')){
        actions += '<button class="btn sm" onclick="recruitQuickFix(' +
          it.application_id + ')">归属</button>';
      }
      if(it.candidate_id){
        actions += '<button class="btn sm" onclick="recruitOpenCandidate(' +
          it.candidate_id + ')">档案</button>';
      }
      return '<div class="rp-health-item"><div><b>' + esc(it.name || '—') +
        '</b><div class="m">' + esc(meta) + ' · ' + esc(it.reason || '') +
        (it.suggested_unit ? ' · 建议：' + esc(it.suggested_unit) : '') +
        '</div></div><div class="a">' + actions + '</div></div>';
    }).join('');
    return '<div class="rp-health-card"><h4>' + esc(d0[1]) +
      '<span class="n">' + b.count + '</span></h4><div class="m">' +
      esc(d0[2]) + '</div>' + items + '</div>';
  }).join('');
  var hc = h.job_headcount || {};
  var extra = '<div class="rp-health-card"><h4>岗位招聘数量 vs 年度计划</h4>' +
    '<div class="m">岗位管理里的 headcount 合计 ' + rpNum(hc.count) +
    ' 人；年度应招 ' + rpNum(hc.plan_total) + ' 人；差异 ' +
    rpNum(hc.diff) + ' 人。</div>' +
    '<div class="hint" style="margin-top:6px">两者粒度不同（岗位 vs 单位×学历），' +
    '系统不会自动同步，仅作提示。</div></div>';
  body.innerHTML = '<div class="rp-bar"><span class="hint">数据体检只统计本届；' +
    '带“归属”的条目可直接补录用单位。</span></div>' +
    '<div class="rp-health">' +
    (cards || '<div class="rp-empty">本届没有发现数据问题。</div>') + extra + '</div>';
}

async function recruitQuickFix(aid){
  var unit = await askText('设置录用单位', '', '如：钛所');
  if(!unit || unit === true) return;
  var r = await api('/api/recruit/signings/' + aid + '/hire-unit', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({hire_unit:String(unit).trim()})
  });
  if(r.__err){ toast('保存失败：' + r.__err, 'danger'); return; }
  toast('已更新录用单位');
  await recruitLoad();
}

async function recruitQuickFixDate(aid){
  var d = await askText('补签约时间', '', 'YYYY-MM-DD');
  if(!d || d === true) return;
  var r = await api('/api/recruit/signings/' + aid + '/hire-unit', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({signed_at:String(d).trim()})
  });
  if(r.__err){ toast('保存失败：' + r.__err, 'danger'); return; }
  toast('已补签约时间');
  await recruitLoad();
}

function recruitAskAssistant(){
  var year = RECRUIT.data && RECRUIT.data.year;
  var label = year ? year.label : '本届';
  setView('chat');
  setTimeout(function(){
    var inp = document.getElementById('chatIn');
    if(!inp) return;
    inp.value = '请总结' + label + '的招聘进度：应招总数、已签人数、签约缺口、' +
      '完成率，并按学历和缺口最大的单位说明重点。';
    inp.dispatchEvent(new Event('input', {bubbles:true}));
    var btn = document.getElementById('chatSend');
    if(btn) btn.click();
  }, 80);
}

async function recruitOpenCandidate(cid){
  if(!cid) return;
  setView('pool');
  await pick(cid);
}

function recruitNewYear(){
  var s = (RECRUIT.meta && RECRUIT.meta.suggested) || {};
  openProgDlg('新建届别',
    '<div class="rp-form">' +
      '<label>届别名称</label>' +
      '<input id="rnLabel" placeholder="如：2028届" value="' + esc(s.label || '') + '">' +
      '<label>届期（开始 ~ 结束）</label>' +
      '<div class="rp-date-range">' +
        '<input id="rnStart" type="date" value="' +
          esc(s.period_start || '') + '">' +
        '<span class="dash">~</span>' +
        '<input id="rnEnd" type="date" value="' +
          esc(s.period_end || '') + '">' +
      '</div>' +
      '<label>单位称呼（总结句用）</label>' +
      '<input id="rnOrg" value="' + esc(s.org_label || '院本部') + '" placeholder="如：院本部">' +
    '</div>' +
    '<div class="bar" style="margin-top:16px;justify-content:flex-end">' +
      '<button class="btn" onclick="closeProgDlg()">取消</button>' +
      '<button class="btn primary" onclick="recruitNewYearCreate()">创建</button>' +
    '</div>');
}

async function recruitNewYearCreate(){
  var v = function(id){
    var el = document.getElementById(id);
    return el ? String(el.value || '').trim() : '';
  };
  var label = v('rnLabel'), start = v('rnStart'), end = v('rnEnd');
  var org = v('rnOrg') || '院本部';
  if(!label){ toast('请填届别名称'); return; }
  if(!start || !end){ toast('请选择开始和结束日期'); return; }
  var r = await api('/api/recruit/years', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({label:label, period_start:start, period_end:end,
                         org_label:org})
  });
  if(r.__err){ toast('创建失败：' + r.__err, 'danger'); return; }
  closeProgDlg();
  toast('已创建 ' + label);
  RECRUIT.yearId = r.id;
  RECRUIT.data = null; RECRUIT.roster = null;
  await initRecruit();
}

function recruitImport(){
  var inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.xlsx';
  inp.onchange = async function(){
    var f = inp.files && inp.files[0];
    if(!f) return;
    RECRUIT.pendingFile = f;
    toast('正在解析 Excel…');
    var r = await fetch('/api/recruit/import?dry_run=1&name=' +
        encodeURIComponent(f.name),
      {method:'POST', headers:hdr({'Content-Type':'application/octet-stream'}),
       body:await f.arrayBuffer()});
    var j = await r.json().catch(function(){ return {}; });
    if(!r.ok){ toast(j.detail || ('解析失败 HTTP ' + r.status), 'danger'); return; }
    var rows = j.rows || [];
    var total = rows.reduce(function(a, x){
      return a + Number(x.plan_approved || 0) + Number(x.plan_extra || 0) +
             Number(x.plan_carryover || 0) - Number(x.plan_cancel || 0);
    }, 0);
    var head = '<div style="margin-bottom:8px">解析到 <b>' + rows.length +
      '</b> 行计划，合计应招 <b>' + total + '</b> 人。' +
      (j.title_period ? '<span class="hint">文件标题届期：' +
        esc(j.title_period.period_start) + ' 至 ' +
        esc(j.title_period.period_end) + '</span>' : '') + '</div>';
    var warn = (j.warnings || []).map(function(x){
      return '<div class="hint">· ' + esc(x) + '</div>';
    }).join('');
    var errs = (j.errors || []).slice(0, 10).map(function(x){
      return '<div style="color:var(--bad)">· ' + esc(x) + '</div>';
    }).join('');
    var table = '<div style="max-height:300px;overflow:auto;margin-top:8px">' +
      '<table class="rp-import-table"><thead><tr><th>单位</th><th>学历</th>' +
      '<th>审批</th><th>追加</th><th>转上年度</th><th>应取消</th><th>应招</th>' +
      '</tr></thead><tbody>' + rows.slice(0, 100).map(function(x){
        return '<tr><td>' + esc(x.unit_name) + '</td><td>' + esc(x.edu_level) +
          '</td><td>' + rpNum(x.plan_approved) + '</td><td>' + rpNum(x.plan_extra) +
          '</td><td>' + rpNum(x.plan_carryover) + '</td><td>' +
          rpNum(x.plan_cancel) + '</td><td>' +
          rpNum(x.plan_approved + x.plan_extra + x.plan_carryover - x.plan_cancel) +
          '</td></tr>';
      }).join('') + '</tbody></table></div>';
    openProgDlg('导入招聘计划（预览）', head + warn + errs + table +
      '<div class="bar" style="margin-top:14px;justify-content:flex-end">' +
      '<button class="btn" onclick="closeProgDlg()">取消</button>' +
      '<button class="btn" onclick="recruitApplyImport(\'merge\')">合并导入（只补缺行）</button>' +
      '<button class="btn primary" onclick="recruitApplyImport(\'overwrite\')">覆盖导入</button></div>');
  };
  inp.click();
}

async function recruitApplyImport(mode){
  var f = RECRUIT.pendingFile;
  if(!f){ toast('请重新选择文件', 'danger'); return; }
  var q = '/api/recruit/import?dry_run=0&mode=' + encodeURIComponent(mode) +
          '&name=' + encodeURIComponent(f.name);
  var y = RECRUIT.data && RECRUIT.data.year;
  if(y){
    q += '&year_id=' + encodeURIComponent(y.id);
  }else{
    var s = (RECRUIT.meta && RECRUIT.meta.suggested) || {};
    q += '&label=' + encodeURIComponent(s.label || '') +
         '&period_start=' + encodeURIComponent(s.period_start || '') +
         '&period_end=' + encodeURIComponent(s.period_end || '');
  }
  var r = await fetch(q, {
    method:'POST', headers:hdr({'Content-Type':'application/octet-stream'}),
    body:await f.arrayBuffer()});
  var j = await r.json().catch(function(){ return {}; });
  if(!r.ok){ toast(j.detail || ('导入失败 HTTP ' + r.status), 'danger'); return; }
  closeProgDlg();
  toast('导入完成：新增 ' + j.inserted + ' 行，覆盖 ' + j.updated + ' 行', 'ok');
  if(j.year && j.year.id) RECRUIT.yearId = j.year.id;
  RECRUIT.pendingFile = null;
  RECRUIT.data = null; RECRUIT.roster = null;
  await initRecruit();
}

async function recruitExport(){
  var sel = document.getElementById('rpExportKind');
  var kind = sel ? sel.value : 'official';
  var q = 'year_id=' + encodeURIComponent(RECRUIT.yearId);
  if(kind === 'roster'){
    var f = RECRUIT.rosterFilters || {};
    Object.keys(f).forEach(function(k){
      if(f[k]) q += '&' + k + '=' + encodeURIComponent(f[k]);
    });
    q = '/api/recruit/roster/export?' + q;
  }else{
    /* 官方/平铺进度表始终导出全届：页面的单位/学历筛选是“看”的，
       带进导出会让官方表只出现一部分单位，容易被当成全貌。 */
    q = '/api/recruit/export?' + q + '&fmt=' + encodeURIComponent(kind);
  }
  toast('正在导出…');
  var r = await fetch(q, {headers:hdr({})});
  if(!r.ok){
    var j = await r.json().catch(function(){ return {}; });
    toast(j.detail || ('导出失败 HTTP ' + r.status), 'danger');
    return;
  }
  var cd = r.headers.get('Content-Disposition') || '';
  var m = /filename\*=UTF-8''([^;]+)/.exec(cd);
  var name = m ? decodeURIComponent(m[1]) : '招聘进度.xlsx';
  saveBlob(await r.blob(), name);
  toast('已导出');
}

async function userCreate(){
  var name = await askText('新建账号', '', '账号名（登录用，不能再改）');
  if(!name || name === true) return;
  var pw = await askText('设置初始密码', '', '至少 8 位');
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
  var pw = await askText('重置「' + username + '」的密码', '', '至少 8 位');
  if(!pw || pw === true) return;
  var r = await api('/api/users/' + encodeURIComponent(username) + '/password',
    {method:'POST', headers:{'Content-Type':'application/json'},
     body: JSON.stringify({new_password: String(pw)})});
  if(r.__err){ toast('重置失败：' + r.__err, 'danger'); return; }
  toast('已重置密码。该账号需要重新登录。');
}

/* ---------- 设置弹窗（v5.2） ----------
   不新增一个主页面：复用原来的系统配置卡片，按 data-g / 标题分成模块。
   UI 颜色、圆角、输入框全部沿用现有 token；这里只负责“装进弹窗 + 分栏”。 */
var SETTINGS_CAT = 'model';
var SETTINGS_CATS = [
  ['model', '模型与密钥'],
  ['mail', '邮件与模板'],
  ['dict', '业务字典'],
  ['data', '数据与运行'],
  ['users', '账号与权限'],
  ['appearance', '外观']
];

function settingsCatOf(el){
  if(!el) return '';
  if(el.id === 'settingsUsers') return 'users';
  if(el.id === 'settingsAppearance') return 'appearance';
  var g = el.dataset ? el.dataset.g : '';
  if(g === 'model' || g === 'embed') return 'model';
  if(g === 'smtp' || g === 'mb') return 'mail';
  if(g === 'dict') return 'dict';
  if(g === 'sources' || g === 'settings') return 'data';
  var t = (el.textContent || '');
  if(t.indexOf('模板管理') >= 0) return 'mail';
  if(t.indexOf('数据存放') >= 0 || t.indexOf('运行环境') >= 0) return 'data';
  return '';
}

function ensureSettingsExtras(){
  var body = document.getElementById('settingsBody');
  if(!body) return;
  if(!document.getElementById('settingsUsers')){
    body.insertAdjacentHTML('beforeend',
      '<div id="settingsUsers" style="display:none"></div>');
  }
  if(!document.getElementById('settingsAppearance')){
    body.insertAdjacentHTML('beforeend',
      '<div id="settingsAppearance" style="display:none">' +
      '<div class="sec"><h3>外观</h3><p class="hint">设置只影响当前浏览器，不改业务数据。</p>' +
      '<button class="btn" onclick="toggleTheme()">切换深色 / 浅色</button></div></div>');
  }
}

function buildSettingsNav(){
  var nav = document.getElementById('settingsNav');
  if(!nav) return;
  var cats = SETTINGS_CATS.filter(function(c){
    /* 本机模式的 `can()` 为了业务操作放行所有权限，但账号管理是例外：
       服务端仍按真实角色校验，hr 没有 manage_users。这里按 ME 的真实
       权限过滤，避免显示一个点进去只有 403 的“账号与权限”。 */
    if(c[0] === 'users') {
      return !!(ME && (ME.permissions || []).indexOf('manage_users') >= 0);
    }
    return true;
  });
  nav.innerHTML = '<h3>设置</h3>' + cats.map(function(c){
    return '<button type="button" data-scat="' + c[0] + '"' +
      (c[0] === SETTINGS_CAT ? ' class="on"' : '') + '>' + esc(c[1]) + '</button>';
  }).join('');
  Array.prototype.forEach.call(nav.querySelectorAll('[data-scat]'), function(b){
    b.addEventListener('click', function(){
      SETTINGS_CAT = b.dataset.scat;
      Array.prototype.forEach.call(nav.querySelectorAll('[data-scat]'), function(x){
        x.classList.toggle('on', x === b);
      });
      applySettingsCat();
    });
  });
}

function applySettingsCat(){
  var body = document.getElementById('settingsBody');
  if(!body) return;
  ensureSettingsExtras();
  Array.prototype.forEach.call(body.children, function(el){
    var cat = settingsCatOf(el);
    if(!cat){ el.style.display = 'none'; return; }
    el.style.display = (cat === SETTINGS_CAT) ? '' : 'none';
  });
  var users = document.getElementById('settingsUsers');
  if(users && SETTINGS_CAT === 'users'){
    users.style.display = '';
    initUsers('settingsUsers');
  }
  var app = document.getElementById('settingsAppearance');
  if(app && SETTINGS_CAT === 'appearance') app.style.display = '';
}

async function openSettings(cat){
  if(!can('settings')){ toast('没有设置权限'); return; }
  SETTINGS_CAT = cat || 'model';
  if(SETTINGS_CAT === 'users' &&
     !(ME && (ME.permissions || []).indexOf('manage_users') >= 0)) SETTINGS_CAT = 'model';
  var mask = document.getElementById('settingsMask');
  if(!mask) return;
  mask.classList.add('on');
  await initSys('settingsBody');
  ensureSettingsExtras();
  buildSettingsNav();
  applySettingsCat();
}

function closeSettings(){
  var mask = document.getElementById('settingsMask');
  if(mask) mask.classList.remove('on');
  SYS_TARGET = 'sysView';
}

document.addEventListener('keydown', function(ev){
  if(ev.key === 'Escape'){
    /* 设置里打开的新建/编辑模板弹窗优先级更高：先让 `#mask` 处理，
       不要把设置和业务弹窗一起关掉。 */
    var dlg = document.getElementById('mask');
    if(dlg && dlg.classList.contains('on')) return;
    var mask = document.getElementById('settingsMask');
    if(mask && mask.classList.contains('on')) closeSettings();
  }
});

async function initSys(target){
  if(target) SYS_TARGET = target;
  var box = document.getElementById(SYS_TARGET) || document.getElementById('sysView');
  if(!box) return;
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
      /* 启用开关：关闭后所有 AI 功能走规则/不可用；以前只能改 config/model.json。 */
      cfgEdit('mcEnabled', '启用模型', m.enabled ? '1' : '0', '', 'select', false, null,
              [['1', '开'], ['0', '关']]) +
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
      cfgRow('', '<button class="btn sm" data-cfg="embFromModel">接口地址同对话模型</button>' +
        '<span class="hint" style="margin-left:8px">两处常是同一个服务，省得再敲一遍</span>') +
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
      cfgRow('', '<button class="btn sm" data-cfg="smtpFromImap">账号同收信配置</button>' +
        '<span class="hint" style="margin-left:8px">授权码取不回来，两边仍需各填一次</span>') +
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
      cfgRow('', '<button class="btn sm" data-cfg="imapFromSmtp">账号同发信配置</button>') +
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
      cfgEdit('cfgMbReapply', '同岗位重投窗口(天，兼容项)',
              (mb.same_job_reapply_days == null ? 365 : mb.same_job_reapply_days),
              '365', 'number') +
      '<div style="margin-top:10px"><button class="btn" data-cfg="mbTest">测试收信</button>' +
        '<span class="msgline busy" id="mbMsg" style="display:none"></span></div>' +
      '<div style="margin-top:8px"><button class="btn" onclick="previewMailbox()">预览收信</button>' +
        '<span class="msgline busy" id="mbPvMsg" style="display:none"></span></div>' +
      '<div id="mbPvBox" style="display:none;margin-top:8px"></div>' +
    '</div>' +

    '<div class="sec cfgsec" data-g="dict"><h3>面试字典' + '<span class="cfgacts">' + '<button class="btn sm" data-cfg-g="edit">修改</button>' + '<button class="btn primary sm" data-cfg-g="save" style="display:none">保存</button>' + '<button class="btn sm" data-cfg-g="cancel" style="display:none">取消</button>' + '</span></h3>' +
      dictItemsRow('units', '面试单位', dc.units) +
      '<div class="dict-bulk">' + cfgEdit('cfgUnits', '面试单位', dictLines(dc.units), '一行一个', 'textarea') + '</div>' +
      dictItemsRow('rooms', '会议室', dc.rooms) +
      '<div class="dict-bulk">' + cfgEdit('cfgRooms', '会议室', dictLines(dc.rooms), '一行一个', 'textarea') + '</div>' +
      dictItemsRow('slots', '面试时段', dc.slots) +
      '<div class="dict-bulk">' + cfgEdit('cfgSlots', '面试时段', dictLines(dc.slots), '一行一个', 'textarea') + '</div>' +
      dictItemsRow('modes', '面试方式', dc.modes) +
      '<div class="dict-bulk">' + cfgEdit('cfgModes', '面试方式', dictLines(dc.modes), '一行一个', 'textarea') + '</div>' +
      dictItemsRow('contacts', '联系人', (dc.contacts || []).map(function(c){
        return [c.dept, c.name, c.phone].filter(Boolean).join(' · '); })) +
      '<div class="dict-bulk">' + cfgEdit('cfgContacts', '联系人', contactLines, '部门 | 姓名 | 电话，一行一个', 'textarea') + '</div>' +
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
  /* 面试字典的"输入后回车添加"：**必须在这里绑 keydown**。
     2026-11-02 修的 bug：输入框一直写着"输入后回车添加"，但代码里从来没有
     keydown 监听 —— 用户敲回车什么都不会发生，只能猜。 */
  Array.prototype.forEach.call(box.querySelectorAll('.dict-add'), function(inp){
    inp.addEventListener('keydown', function(ev){
      if(ev.key === 'Enter'){
        ev.preventDefault();
        dictAdd(inp.id.replace('dictAdd_', ''));
      }
    });
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
  if(SYS_TARGET === 'settingsBody') applySettingsCat();
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
/* 把值填进某个配置输入框：**只填不存**，用户仍要自己点「保存」。
   2026-11-02 加，用于「账号同收信 / 接口地址同对话模型」这类少填一遍的按钮。 */
function fillCfg(id, val){
  var e = document.getElementById(id);
  if(!e) return false;
  e.value = (val == null ? '' : String(val));
  try{ e.dispatchEvent(new Event('input', {bubbles:true})); }catch(err){}
  return true;
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
  if(act === 'embFromModel'){
    var _bu = v('mcBaseUrl');
    if(!_bu){ toast('对话模型的接口地址还没填', 'warn'); return; }
    fillCfg('embBaseUrl', _bu);
    toast('已带入对话模型的接口地址，记得点「保存」', 'ok');
    return;
  }
  if(act === 'smtpFromImap' || act === 'imapFromSmtp'){
    var fromId = act === 'smtpFromImap' ? 'cfgMbUser' : 'cfgSmtpUser';
    var toId = act === 'smtpFromImap' ? 'cfgSmtpUser' : 'cfgMbUser';
    var _u = v(fromId);
    if(!_u){ toast('对侧还没填账号', 'warn'); return; }
    fillCfg(toId, _u);
    toast('已带入账号，记得点「保存」', 'ok');
    return;
  }
  if(act === 'model'){
    var body = {base_url: v('mcBaseUrl'), model: v('mcModel'),
                enabled: v('mcEnabled') === '1'};
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
    /* 直接打开**完整编辑器**（2026-10-28）：原来是三个连续的 askText 弹窗
       （名称 → 主题 → 正文），一个个弹、还要在单行文本框里写正文 ——
       而下面这个按钮旁边就有带富文本的编辑页，两条路径体验完全不一致。
       现在统一走 editTpl(0)（isNew = true），一次弹全。 */
    editTpl(0);
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
        /* 模板编辑器与写邮件共用同一套“先选行列再插真表格”的小型选择器；
           原来这里调 tplInsertTable() 固定插 2 列 3 行，HR 无法选行列。 */
        '<button type="button" class="btn sm" title="插入表格" onclick="insertTableDialog(\'tplEditor\', this)">▦ 表格</button>' +
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
/* 当前对话累计的产出（下载/草稿/提案），不随新一轮回答覆盖 */
var CHAT_OUTPUTS = [];
/* v4.0.17：当前会话 id、会话列表、是否已完成首次进入初始化 */
var CHAT_CONV_ID = '', CHAT_CONVS = [], CHAT_BOOTED = false;
/* ---------- Mermaid 图表 → 内联 SVG ----------
   模型按 Mermaid 语法输出图表（pie / xychart-beta）。内网/离线环境不能加载
   mermaid.js，所以这里只实现这两类最常用的图，直接生成 SVG：不依赖外部库、
   不联网，颜色与文字跟随深/浅色主题。 */
function chartEsc(s){
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function chartUnesc(s){
  return String(s == null ? '' : s).replace(/&lt;/g,'<').replace(/&gt;/g,'>')
    .replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&');
}
function chartStripQuotes(s){
  return String(s == null ? '' : s).trim().replace(/^["']|["']$/g,'').trim();
}
function chartShort(s, n){
  s = String(s == null ? '' : s);
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}
function chartSplitList(s){
  var out = [], re = /"([^"]*)"|'([^']*)'|([^,]+)/g, m;
  while((m = re.exec(String(s || '')))){
    var v = (m[1] !== undefined ? m[1] : (m[2] !== undefined ? m[2] : m[3]));
    v = chartStripQuotes(v);
    if(v !== '') out.push(v);
  }
  return out;
}
function chartNiceMax(v){
  if(!isFinite(v) || v <= 0) return 1;
  var p = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
  var n = v / p;
  var step = n <= 1 ? 1 : (n <= 2 ? 2 : (n <= 5 ? 5 : 10));
  return step * p;
}
var CHART_COLORS = ['#C56A45','#4E79A7','#59A14F','#E15759','#B07AA1',
                    '#76B7B2','#EDC948','#9C755F'];

function parseMermaidPie(code){
  var title = '', rows = [];
  String(code || '').split(/\r?\n/).forEach(function(line){
    var t = line.trim();
    if(!t) return;
    if(/^pie\b/i.test(t)){
      var mt = t.match(/title\s+(.+)$/i);
      if(mt) title = chartStripQuotes(mt[1]);
      return;
    }
    var m = t.match(/^title\s+(.+)$/i);
    if(m){ title = chartStripQuotes(m[1]); return; }
    m = t.match(/^["']?(.+?)["']?\s*:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*$/);
    if(m) rows.push({label: chartStripQuotes(m[1]), value: parseFloat(m[2])});
  });
  return {title: title, rows: rows.filter(function(r){ return r.value > 0; })};
}
function pieChartSvg(code){
  var p = parseMermaidPie(code), rows = p.rows;
  if(!rows.length) return null;
  var total = rows.reduce(function(a, r){ return a + r.value; }, 0);
  if(!total) return null;
  var W = 620, H = Math.max(300, 84 + rows.length * 26);
  var cx = 176, cy = H / 2 + 8, r = Math.min(104, H / 2 - 58);
  var paths = '', labels = '', a0 = -Math.PI / 2;
  if(rows.length === 1){
    paths = '<circle cx="' + cx + '" cy="' + cy + '" r="' + r +
      '" fill="' + CHART_COLORS[0] + '"></circle>';
  } else {
    rows.forEach(function(row, i){
      var frac = row.value / total, a1 = a0 + frac * Math.PI * 2;
      var x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
      var x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
      var large = frac > 0.5 ? 1 : 0;
      paths += '<path d="M' + cx + ' ' + cy + ' L' + x0 + ' ' + y0 +
        ' A' + r + ' ' + r + ' 0 ' + large + ' 1 ' + x1 + ' ' + y1 + ' Z" fill="' +
        CHART_COLORS[i % CHART_COLORS.length] + '" stroke="var(--surface,#fff)"' +
        ' stroke-width="2"></path>';
      if(frac >= 0.055){
        var am = (a0 + a1) / 2;
        var lx = cx + r * 0.62 * Math.cos(am), ly = cy + r * 0.62 * Math.sin(am);
        labels += '<text x="' + lx + '" y="' + ly + '" text-anchor="middle"' +
          ' dominant-baseline="middle" font-size="12" font-weight="600"' +
          ' fill="#fff">' + (frac * 100).toFixed(1) + '%</text>';
      }
      a0 = a1;
    });
  }
  var legendX = 318, legendY = Math.max(50, (H - rows.length * 26) / 2 + 8);
  var legend = rows.map(function(row, i){
    var y = legendY + i * 26;
    return '<rect x="' + legendX + '" y="' + (y - 10) + '" width="12" height="12"' +
      ' rx="3" fill="' + CHART_COLORS[i % CHART_COLORS.length] + '"></rect>' +
      '<text x="' + (legendX + 20) + '" y="' + y + '" font-size="13"' +
      ' fill="var(--ink,#333)">' + chartEsc(chartShort(row.label, 13)) + '</text>' +
      '<text x="' + (W - 22) + '" y="' + y + '" text-anchor="end" font-size="12.5"' +
      ' fill="var(--ink-2,#666)">' + row.value + '（' +
      (row.value / total * 100).toFixed(1) + '%）</text>';
  }).join('');
  var title = p.title ? '<text x="' + (W / 2) + '" y="26" text-anchor="middle"' +
    ' font-size="15" font-weight="600" fill="var(--ink,#222)">' +
    chartEsc(p.title) + '</text>' : '';
  return '<div class="mchart"><div class="mchart-bar">' +
    '<button class="mdt-btn" data-chart-svg>下载 SVG</button></div>' +
    '<svg viewBox="0 0 ' + W + ' ' + H + '"' +
    ' role="img" aria-label="' + chartEsc(p.title || '饼图') + '">' +
    title + paths + labels + legend + '</svg></div>';
}

function parseXYChart(code){
  var o = {title:'', x:[], yLabel:'', yMin:0, yMax:null, series:[]};
  String(code || '').split(/\r?\n/).forEach(function(line){
    var t = line.trim();
    if(!t || /^xychart(-beta)?$/i.test(t) || t.indexOf('%%') === 0) return;
    var m = t.match(/^title\s+(.+)$/i);
    if(m){ o.title = chartStripQuotes(m[1]); return; }
    m = t.match(/^x-axis\s*\[(.*)\]\s*$/i);
    if(m){ o.x = chartSplitList(m[1]); return; }
    m = t.match(/^y-axis\s*(?:"([^"]*)")?\s*(-?[0-9]+(?:\.[0-9]+)?)?\s*-->\s*(-?[0-9]+(?:\.[0-9]+)?)/i);
    if(m){
      o.yLabel = m[1] || '';
      if(m[2] !== undefined) o.yMin = parseFloat(m[2]);
      if(m[3] !== undefined) o.yMax = parseFloat(m[3]);
      return;
    }
    m = t.match(/^(line|bar)\s*\[(.*)\]\s*$/i);
    if(m){
      o.series.push({type: m[1].toLowerCase(),
        values: chartSplitList(m[2]).map(function(v){ return Number(v); })
          .filter(function(v){ return isFinite(v); })});
    }
  });
  return o;
}
function xyChartSvg(code){
  var o = parseXYChart(code);
  if(!o.series.length) return null;
  var n = Math.max(o.x.length, o.series.reduce(function(a, s){
    return Math.max(a, s.values.length);
  }, 0));
  if(!n) return null;
  var all = [];
  o.series.forEach(function(s){ all = all.concat(s.values); });
  var yMax = (o.yMax !== null && isFinite(o.yMax) && o.yMax > o.yMin)
    ? o.yMax : chartNiceMax(Math.max.apply(null, all.concat([1])));
  var yMin = isFinite(o.yMin) ? o.yMin : 0;
  var W = 620, H = 350, padL = 58, padR = 30, padT = 54, padB = 58;
  var plotW = W - padL - padR, plotH = H - padT - padB;
  var yOf = function(v){ return padT + plotH * (1 - (v - yMin) / (yMax - yMin)); };
  var xOf = function(i){ return padL + plotW * (i + 0.5) / n; };
  var grid = '';
  for(var g = 0; g <= 4; g++){
    var val = yMin + (yMax - yMin) * g / 4;
    var gy = yOf(val);
    grid += '<line x1="' + padL + '" y1="' + gy + '" x2="' + (W - padR) +
      '" y2="' + gy + '" stroke="var(--line,#e3e3e3)" stroke-width="1"></line>' +
      '<text x="' + (padL - 8) + '" y="' + (gy + 4) + '" text-anchor="end"' +
      ' font-size="11" fill="var(--ink-3,#888)">' +
      (Math.round(val * 10) / 10) + '</text>';
  }
  var bars = '', lines = '', dots = '';
  var barSeries = o.series.filter(function(s){ return s.type === 'bar'; });
  var barGroup = plotW / n * 0.62;
  var barW = barSeries.length ? barGroup / barSeries.length : 0;
  o.series.forEach(function(s, si){
    var color = CHART_COLORS[si % CHART_COLORS.length];
    if(s.type === 'bar'){
      var bi = barSeries.indexOf(s);
      s.values.forEach(function(v, i){
        var x = padL + plotW * (i + 0.5) / n - barGroup / 2 + bi * barW;
        var y = yOf(v);
        bars += '<rect x="' + x + '" y="' + y + '" width="' + Math.max(2, barW - 3) +
          '" height="' + Math.max(0, padT + plotH - y) + '" rx="3" fill="' +
          color + '"></rect>';
      });
    } else {
      var pts = s.values.map(function(v, i){ return xOf(i) + ',' + yOf(v); }).join(' ');
      lines += '<polyline points="' + pts + '" fill="none" stroke="' + color +
        '" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"></polyline>';
      s.values.forEach(function(v, i){
        var x = xOf(i), y = yOf(v);
        dots += '<circle cx="' + x + '" cy="' + y + '" r="3.6" fill="' + color +
          '" stroke="var(--surface,#fff)" stroke-width="1.5"></circle>';
        if(n <= 12){
          dots += '<text x="' + x + '" y="' + (y - 9) + '" text-anchor="middle"' +
            ' font-size="11" fill="var(--ink-2,#666)">' + v + '</text>';
        }
      });
    }
  });
  var xLabels = '';
  for(var i = 0; i < n; i++){
    var label = o.x[i] !== undefined ? o.x[i] : String(i + 1);
    xLabels += '<text x="' + (padL + plotW * (i + 0.5) / n) + '" y="' + (H - 32) +
      '" text-anchor="middle" font-size="' + (n > 10 ? 10 : 11.5) +
      '" fill="var(--ink-2,#666)">' + chartEsc(chartShort(label, 8)) + '</text>';
  }
  var legend = o.series.map(function(s, si){
    return '<rect x="' + (W - 128 + si * 62) + '" y="14" width="10" height="10"' +
      ' rx="2" fill="' + CHART_COLORS[si % CHART_COLORS.length] + '"></rect>' +
      '<text x="' + (W - 114 + si * 62) + '" y="23" font-size="11.5"' +
      ' fill="var(--ink-2,#666)">' + (s.type === 'bar' ? '柱状' : '折线') + '</text>';
  }).join('');
  var title = o.title ? '<text x="' + padL + '" y="26" font-size="15"' +
    ' font-weight="600" fill="var(--ink,#222)">' + chartEsc(o.title) + '</text>' : '';
  var yLabel = o.yLabel ? '<text x="' + padL + '" y="' + (padT - 4) + '"' +
    ' font-size="11.5" fill="var(--ink-3,#888)">' + chartEsc(o.yLabel) + '</text>' : '';
  return '<div class="mchart"><div class="mchart-bar">' +
    '<button class="mdt-btn" data-chart-svg>下载 SVG</button></div>' +
    '<svg viewBox="0 0 ' + W + ' ' + H + '"' +
    ' role="img" aria-label="' + chartEsc(o.title || '图表') + '">' +
    title + legend + yLabel + grid + bars + lines + dots + xLabels +
    '<line x1="' + padL + '" y1="' + (padT + plotH) + '" x2="' + (W - padR) +
    '" y2="' + (padT + plotH) + '" stroke="var(--line-2,#ccc)" stroke-width="1"></line>' +
    '</svg></div>';
}
function mermaidChartSvg(raw){
  var code = chartUnesc(raw);
  var first = '';
  var ls = code.split(/\r?\n/);
  for(var i = 0; i < ls.length; i++){
    var t = ls[i].trim();
    if(t && t.indexOf('%%') !== 0){ first = t.toLowerCase(); break; }
  }
  if(first.indexOf('pie') === 0) return pieChartSvg(code);
  if(first.indexOf('xychart') === 0) return xyChartSvg(code);
  return null;
}

/* ---------- 日程安排（v4.4.3） ----------
   数据源唯一：applications.stage == '待面试' 且 interview_date 非空。
   月/周/日三种视图都从同一个 /api/interviews/calendar 拉当前范围；
   拖拽只改 interview_date / interview_slot，不改阶段，也不自动归档。 */
function schedPad(n){ return (n < 10 ? '0' : '') + n; }
function schedYmd(d){
  return d.getFullYear() + '-' + schedPad(d.getMonth() + 1) + '-' + schedPad(d.getDate());
}
function schedAtMidnight(d){ return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
function schedAddDays(d, n){
  var x = schedAtMidnight(d); x.setDate(x.getDate() + n); return x;
}
function schedMonday(d){
  var x = schedAtMidnight(d);
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}
function schedDateOf(s){
  var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ''));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}
function schedTimeOf(s){
  var m = /^\d{4}-\d{2}-\d{2}[ T](\d{2}):(\d{2})/.exec(String(s || ''));
  return m ? (m[1] + ':' + m[2]) : '';
}
/* 历史档案的 interview_date 常常只存了日期，真正的时段在 interview_slot
   （如「9:00-9:50」「上午 9:00-9:30」）。只认 interview_date 会把所有面试
   都堆到「全天」一行，所以开始/结束时间必须回退到时段原文。 */
function schedSlotRange(slot){
  var s = String(slot || '').replace(/：/g, ':').replace(/[～~—－]/g, '-');
  var ap = /下午|晚上|傍晚/.test(s) ? 'pm' : (/上午|早上|早晨/.test(s) ? 'am' : '');
  var re = /(\d{1,2})\s*[:点时]\s*(\d{1,2})?/g;
  var out = [], m;
  while((m = re.exec(s)) && out.length < 2){
    var h = Number(m[1]), mi = Number(m[2] || 0);
    if(h > 24 || mi > 59) continue;
    if(ap === 'pm' && h < 12) h += 12;
    if(ap === 'am' && h === 12) h = 0;
    out.push(schedPad(h) + ':' + schedPad(mi));
  }
  return {start: out[0] || '', end: out[1] || ''};
}
function schedEventStart(ev){
  var t = schedTimeOf(ev && ev.interview_date);
  /* 00:00 是旧数据常见的“只有日期”占位，不应把它当成凌晨面试。 */
  if(t && t !== '00:00') return t;
  return schedSlotRange(ev && ev.interview_slot).start;
}
function schedEventEnd(ev){
  var start = schedEventStart(ev);
  if(!start) return '';
  var r = schedSlotRange(ev && ev.interview_slot);
  /* 原文写了完整时段就按原文结束时间，拖拽只改过开始时刻时按学历时长推。 */
  if(r.end && (!r.start || r.start === start) && r.end !== start) return r.end;
  return schedEndOf(start, schedDurationMinutes(ev && ev.edu_level));
}
function schedMinutesOf(t){
  var m = /^(\d{1,2}):(\d{2})$/.exec(String(t || ''));
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}
function schedEventSpan(ev){
  var a = schedMinutesOf(schedEventStart(ev));
  var b = schedMinutesOf(schedEventEnd(ev));
  if(a != null && b != null && b > a) return b - a;
  return schedDurationMinutes(ev && ev.edu_level);
}
/* 同一天里时间重叠的面试要并排，而不是叠在同一个绝对定位条上。
   做法：按开始时间排序，传递重叠的放进同一组，组内贪心分 lane
   （放进第一个已经结束的 lane，否则新开一列）。 */
function schedLaneLayout(list){
  var items = list.map(function(e){
    var s = schedMinutesOf(schedEventStart(e));
    var span = schedEventSpan(e);
    var start = (s == null ? 0 : s);
    return {ev:e, s:start, e:start + (span || 0), lane:0, lanes:1};
  }).sort(function(a, b){
    return a.s - b.s ||
      String(a.ev.candidate_name || '').localeCompare(String(b.ev.candidate_name || ''), 'zh');
  });
  var i = 0;
  while(i < items.length){
    var cluster = [items[i]];
    var clusterEnd = items[i].e;
    var j = i + 1;
    while(j < items.length && items[j].s < clusterEnd){
      cluster.push(items[j]);
      clusterEnd = Math.max(clusterEnd, items[j].e);
      j++;
    }
    var laneEnds = [];
    cluster.forEach(function(it){
      var lane = -1;
      for(var k = 0; k < laneEnds.length; k++){
        if(laneEnds[k] <= it.s){ lane = k; break; }
      }
      if(lane < 0){
        lane = laneEnds.length;
        laneEnds.push(it.e);
      }else{
        laneEnds[lane] = it.e;
      }
      it.lane = lane;
    });
    cluster.forEach(function(it){ it.lanes = laneEnds.length; });
    i = j;
  }
  return items;
}
/* 周视图里让有并发的日列按 lane 数变宽；日视图只有一列，直接铺满。
   表头 dayhead 和表体 daycol 必须用同一个宽度，否则会错位。 */
function schedDayWidth(lanes){
  if(SCHED.view === 'day') return 'flex:1 1 0;min-width:0';
  var n = Math.max(1, lanes || 1);
  return 'flex:' + n + ' 1 0;min-width:' + Math.max(92, n * 64) + 'px';
}
function schedRangeText(dateStr, slot, edu){
  var ev = {interview_date:dateStr, interview_slot:slot, edu_level:edu};
  var st = schedEventStart(ev), en = schedEventEnd(ev);
  return st ? (st + (en ? '-' + en : '')) : '';
}
/* 8:00–18:00 的十个小时要撑满日程区剩余高度。返回每个小时的高度，
   窗口太矮时保底 46px（滚动），太高时封顶 160px，避免把行拉得离谱。 */
function schedHourHeight(){
  var box = document.getElementById('scheduleView');
  if(!box || !box.clientHeight) return 46;
  var cs = getComputedStyle(box);
  var padT = parseFloat(cs.paddingTop) || 0;
  var padB = parseFloat(cs.paddingBottom) || 0;
  var head = box.querySelector('.sched-head');
  var headH = head ? (head.offsetHeight + 12) : 0;
  var bodyH = box.clientHeight - padT - padB - headH;
  /* 固定占位：星期表头 35 + 全天行 34 + 表格边框 2 + body 底部留白 8。 */
  var h = (bodyH - 79) / 10;
  return Math.max(46, Math.min(160, Math.floor(h)));
}
/* 面试时长按学历：博士 50 分钟，硕士 30 分钟；其他学历按 30 分钟。
   日历用它决定事件块高度，面试信息里的固定时段也按同一口径生成。 */
function schedDurationMinutes(edu){
  var s = String(edu || '');
  if(s.indexOf('博士') >= 0) return 50;
  if(s.indexOf('硕士') >= 0) return 30;
  return 30;
}
function schedEndOf(start, minutes){
  var m = /^(\d{1,2}):(\d{2})$/.exec(String(start || ''));
  if(!m) return '';
  var total = Number(m[1]) * 60 + Number(m[2]) + Number(minutes || 0);
  total = Math.max(0, Math.min(24 * 60 - 1, total));
  return schedPad(Math.floor(total / 60)) + ':' + schedPad(total % 60);
}
/* 面试时段的固定候选：按学历时长生成 8:00–18:00 内的整/半点开始时间，
   再把系统配置里的 slots 合并进来（配置可能有单位自己的作息）。 */
function interviewSlotOptions(edu){
  var dur = schedDurationMinutes(edu);
  var out = [];
  for(var m = 8 * 60; m + dur <= 18 * 60; m += 30){
    out.push(schedPad(Math.floor(m / 60)) + ':' + schedPad(m % 60) + '-' +
             schedPad(Math.floor((m + dur) / 60)) + ':' + schedPad((m + dur) % 60));
  }
  ((typeof IDICT !== 'undefined' && IDICT.slots) || []).forEach(function(x){
    if(x && out.indexOf(x) < 0) out.push(String(x));
  });
  return out;
}
function ivSlotPicked(v){
  var inp = document.getElementById('ivSlot');
  if(!inp) return;
  if(v === '__custom'){
    inp.focus();
    try{ inp.select(); }catch(_e){}
    return;
  }
  if(v) inp.value = v;
}
/* 录用单位的候选来自招聘进度（RECRUIT_UNITS），但用户很可能没先打开过
   招聘进度页；打开档案时按需拉一次，否则 datalist 一直是空的。 */
function hireUnitOptionsHtml(current){
  var units = ((typeof RECRUIT_UNITS !== 'undefined' && RECRUIT_UNITS) || []);
  var cur = String(current || '');
  var out = '<option value="">固定单位…</option>' + units.map(function(u){
    return '<option value="' + esc(u) + '"' + (u === cur ? ' selected' : '') + '>' + esc(u) + '</option>';
  }).join('');
  out += '<option value="__custom"' +
    ((cur && units.indexOf(cur) < 0) ? ' selected' : '') + '>手动输入…</option>';
  return out;
}
function refreshHireUnitOptions(){
  var dl = document.getElementById('dlHireUnit');
  if(dl){
    dl.innerHTML = ((typeof RECRUIT_UNITS !== 'undefined' && RECRUIT_UNITS) || [])
      .map(function(u){ return '<option value="' + esc(u) + '"></option>'; }).join('');
  }
  var sel = document.getElementById('obHireUnitSel');
  var inp = document.getElementById('obHireUnit');
  if(sel){
    var cur = inp ? inp.value : '';
    sel.innerHTML = hireUnitOptionsHtml(cur);
  }
}
function hireUnitPicked(v){
  var inp = document.getElementById('obHireUnit');
  if(!inp) return;
  if(v === '__custom'){
    inp.focus();
    try{ inp.select(); }catch(_e){}
    return;
  }
  if(v) inp.value = v;
}
function ensureHireUnits(){
  if(((typeof RECRUIT_UNITS !== 'undefined' && RECRUIT_UNITS) || []).length){
    refreshHireUnitOptions();
    return;
  }
  if(HIRE_UNITS_LOADED) return;
  HIRE_UNITS_LOADED = true;
  api('/api/recruit/meta').then(function(j){
    if(j && !j.__err){
      RECRUIT_UNITS = j.units || [];
      refreshHireUnitOptions();
      if(!RECRUIT_UNITS.length) HIRE_UNITS_LOADED = false;  /* 还没有计划：下次打开再试 */
    }else{
      HIRE_UNITS_LOADED = false;   /* 失败允许下次打开再试 */
    }
  }).catch(function(){ HIRE_UNITS_LOADED = false; });
}
function schedSameDay(a, b){
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate();
}
function schedRange(){
  var a = SCHED.anchor;
  if(SCHED.view === 'month'){
    var first = new Date(a.getFullYear(), a.getMonth(), 1);
    var last = new Date(a.getFullYear(), a.getMonth() + 1, 0);
    return {from: schedYmd(schedMonday(first)), to: schedYmd(schedAddDays(schedMonday(last), 6))};
  }
  if(SCHED.view === 'week'){
    var mon = schedMonday(a);
    return {from: schedYmd(mon), to: schedYmd(schedAddDays(mon, 6))};
  }
  return {from: schedYmd(a), to: schedYmd(a)};
}
function schedTitle(){
  var a = SCHED.anchor;
  if(SCHED.view === 'month') return a.getFullYear() + '年' + (a.getMonth() + 1) + '月';
  if(SCHED.view === 'week'){
    var mon = schedMonday(a), sun = schedAddDays(mon, 6);
    return (mon.getMonth() + 1) + '月' + mon.getDate() + '日 – ' +
      (sun.getMonth() + 1) + '月' + sun.getDate() + '日';
  }
  var wd = ['日', '一', '二', '三', '四', '五', '六'][a.getDay()];
  return a.getFullYear() + '年' + (a.getMonth() + 1) + '月' + a.getDate() + '日 周' + wd;
}
function schedEventsOn(day){
  var key = schedYmd(day);
  return SCHED.events.filter(function(e){ return String(e.interview_date || '').slice(0, 10) === key; })
    .sort(function(a, b){
      var ta = schedEventStart(a) || '99:99';
      var tb = schedEventStart(b) || '99:99';
      return ta === tb ? String(a.candidate_name || '').localeCompare(String(b.candidate_name || ''), 'zh')
        : ta.localeCompare(tb);
    });
}
function schedChip(ev, timed){
  var t = schedEventStart(ev);
  var end = schedEventEnd(ev);
  var dur = schedEventSpan(ev);
  var bits = [ev.candidate_name || '未识别'];
  if(ev.job_title) bits.push(ev.job_title);
  if(ev.interview_unit) bits.push(ev.interview_unit);
  if(ev.interview_place) bits.push(ev.interview_place);
  if(ev.interview_mode) bits.push(ev.interview_mode);
  var range = t ? (t + (end ? '-' + end : '')) : '';
  var when = range ? (timed ? (range + '（' + dur + '分钟）') : range) : '';
  var title = (when ? when + ' ' : '') + bits.join(' · ');
  var label = (range ? '<span class="t">' + esc(range) + '</span> ' : '') +
    esc(ev.candidate_name || '未识别');
  /* 周/日视图：单位和地点紧跟在姓名后面（同一行，用空白隔开），
     不另起一行；月视图格子太窄，只保留时间 + 姓名。 */
  if(timed){
    var meta = [];
    if(ev.interview_unit) meta.push(ev.interview_unit);
    if(ev.interview_place) meta.push(ev.interview_place);
    if(meta.length){
      label += '<span class="meta">' + meta.map(function(x){
        return '&nbsp;&nbsp;' + esc(x);
      }).join('') + '</span>';
    }
  }
  return '<button type="button" class="sched-ev' + (timed ? ' timed' : '') + '"' +
    ' draggable="' + (can('set_stage') ? 'true' : 'false') + '"' +
    ' data-aid="' + esc(String(ev.application_id)) + '"' +
    ' title="' + esc(title) + '">' + label + '</button>';
}
function schedHeadHtml(){
  var editable = can('set_stage');
  return '<div class="sched-head"><div>' +
    '<div class="sched-title">' + esc(schedTitle()) + '</div>' +
    '<div class="sched-sub">共 ' + SCHED.events.length + ' 场待面试' +
      (editable ? ' · 拖拽卡片可调整日期/时间' : ' · 只读账号不能拖拽') + '</div></div>' +
    '<div class="sched-tools">' +
      '<button class="btn" onclick="schedPrev()" title="上一页">‹</button>' +
      '<button class="btn" onclick="schedToday()">今天</button>' +
      '<button class="btn" onclick="schedNext()" title="下一页">›</button>' +
      '<button class="btn" onclick="schedExportXlsx()">导出 Excel</button>' +
      '<span class="seg">' +
        '<button class="' + (SCHED.view === 'day' ? 'on' : '') + '" onclick="schedSetView(\'day\')">日</button>' +
        '<button class="' + (SCHED.view === 'week' ? 'on' : '') + '" onclick="schedSetView(\'week\')">周</button>' +
        '<button class="' + (SCHED.view === 'month' ? 'on' : '') + '" onclick="schedSetView(\'month\')">月</button>' +
      '</span>' +
      '<button class="btn" onclick="schedLoad()">刷新</button>' +
    '</div></div>';
}
function schedMonthHtml(){
  var start = schedDateOf(SCHED.range.from), end = schedDateOf(SCHED.range.to);
  var today = new Date();
  var html = '<div class="sched-month">' +
    ['一', '二', '三', '四', '五', '六', '日'].map(function(w){
      return '<div class="sched-dow">周' + w + '</div>';
    }).join('');
  var d = schedAtMidnight(start);
  while(d <= end){
    var outside = d.getMonth() !== SCHED.anchor.getMonth();
    var list = schedEventsOn(d);
    var shown = list.slice(0, 3).map(function(e){ return schedChip(e, false); }).join('');
    var more = list.length > 3
      ? '<button class="sched-more" data-more="' + schedYmd(d) + '">+' + (list.length - 3) + ' 场</button>'
      : '';
    html += '<div class="sched-cell' + (outside ? ' out' : '') +
      (schedSameDay(d, today) ? ' today' : '') + '" data-date="' + schedYmd(d) + '">' +
      '<div class="sched-daynum">' + d.getDate() + '</div>' + shown + more + '</div>';
    d = schedAddDays(d, 1);
  }
  return html + '</div>';
}
function schedWeekHtml(){
  var H0 = 8, H1 = 18, HOUR = SCHED.hour || 46;
  var start = schedDateOf(SCHED.range.from);
  var days = [];
  for(var i = 0; i < (SCHED.view === 'day' ? 1 : 7); i++) days.push(schedAddDays(start, i));
  var today = new Date();
  /* 先按天算出 timed 事件的分列结果；表头和表体用同一份 maxLanes，
     否则表头与格子会错位。 */
  var dayData = days.map(function(d){
    var list = schedEventsOn(d);
    var allDay = list.filter(function(e){ return !schedEventStart(e); });
    var timed = schedLaneLayout(list.filter(function(e){ return !!schedEventStart(e); }));
    var lanes = timed.reduce(function(m, it){ return Math.max(m, it.lanes); }, 1);
    return {d:d, allDay:allDay, timed:timed, lanes:lanes};
  });
  var head = '<div class="sched-weekhead"><div class="sched-timecol"></div>' +
    dayData.map(function(dd){
      var d = dd.d;
      var wd = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()];
      return '<div class="sched-dayhead' + (schedSameDay(d, today) ? ' today' : '') +
        '" style="' + schedDayWidth(dd.lanes) + '">周' + wd + ' · ' +
        (d.getMonth() + 1) + '/' + d.getDate() + '</div>';
    }).join('') + '</div>';
  var labels = '<div class="sched-timecol"><div class="sched-allday-label">全天</div>' +
    (function(){
      var out = '';
      for(var h = H0; h < H1; h++){
        out += '<div class="sched-hourlabel" style="height:' + HOUR + 'px">' +
          schedPad(h) + ':00</div>';
      }
      return out;
    })() + '<div class="sched-endtime">' + H1 + ':00</div></div>';
  var cols = dayData.map(function(dd){
    var lines = '';
    for(var h = H0; h < H1; h++) lines += '<div class="sched-hline" style="height:' + HOUR + 'px"></div>';
    var timedHtml = dd.timed.map(function(it){
      var e = it.ev;
      var t = schedEventStart(e).split(':');
      var mins = Number(t[0]) * 60 + Number(t[1]) - H0 * 60;
      var top = Math.max(0, Math.min((H1 - H0) * HOUR - 24, mins / 60 * HOUR));
      var span = schedEventSpan(e);
      var hpx = Math.max(20, Math.min((H1 - H0) * HOUR - top, span / 60 * HOUR));
      var left = (it.lane / it.lanes * 100).toFixed(3);
      var width = (100 / it.lanes).toFixed(3);
      return schedChip(e, true).replace('<button ',
        '<button style="top:' + top.toFixed(0) + 'px;height:' + hpx.toFixed(0) +
        'px;left:calc(' + left + '% + 3px);width:calc(' + width + '% - 6px);right:auto" ');
    }).join('');
    return '<div class="sched-daycol" style="' + schedDayWidth(dd.lanes) +
      '" data-date="' + schedYmd(dd.d) + '">' +
      '<div class="sched-allday">' + dd.allDay.map(function(e){ return schedChip(e, false); }).join('') + '</div>' +
      '<div class="sched-hours">' + lines + timedHtml + '</div></div>';
  }).join('');
  return '<div class="sched-week' + (SCHED.view === 'day' ? ' single' : '') + '">' +
    head + '<div class="sched-weekbody">' + labels + cols + '</div></div>';
}
function renderSchedule(){
  var box = document.getElementById('scheduleView');
  if(!box) return;
  if(SCHED.view === 'month'){
    box.innerHTML = schedHeadHtml() + '<div class="sched-body">' + schedMonthHtml() + '</div>';
  }else{
    /* 先把标题栏和空的 body 铺好，量出真正可用的高度，再按高度生成小时行。
       否则标题栏换行时（窄窗口）会多算/少算几十像素。 */
    box.innerHTML = schedHeadHtml() + '<div class="sched-body"></div>';
    SCHED.hour = schedHourHeight();
    box.querySelector('.sched-body').innerHTML = schedWeekHtml();
  }
  bindSchedule(box);
}
async function schedLoad(){
  var box = document.getElementById('scheduleView');
  if(!box) return;
  var range = schedRange();
  SCHED.range = range;
  var req = ++SCHED.req;
  box.innerHTML = schedHeadHtml() + '<div class="sched-body"><div class="sched-empty">读取日程…</div></div>';
  var r = await api('/api/interviews/calendar?from=' + encodeURIComponent(range.from) +
                    '&to=' + encodeURIComponent(range.to));
  if(req !== SCHED.req) return;
  if(r && r.__err){
    box.innerHTML = schedHeadHtml() + '<div class="sched-body"><div class="sched-empty">' +
      esc(r.__err) + '</div></div>';
    return;
  }
  SCHED.events = (r && r.items) || [];
  renderSchedule();
}
async function initSchedule(){
  if(!SCHED.booted){ SCHED.booted = true; SCHED.anchor = new Date(); }
  if(!window.__schedResizeBound){
    window.__schedResizeBound = true;
    var _rt = null;
    window.addEventListener('resize', function(){
      clearTimeout(_rt);
      _rt = setTimeout(function(){
        var el = document.getElementById('scheduleView');
        if(el && el.classList.contains('on')) renderSchedule();
      }, 120);
    });
  }
  await schedLoad();
}
function schedExportRange(){
  /* 月视图的 SCHED.range 是 6 周网格（包含前后月份的交叉日），导出只要本月。
     周/日视图没有交叉日，直接用当前范围。 */
  if(SCHED.view === 'month'){
    var y = SCHED.anchor.getFullYear(), m = SCHED.anchor.getMonth();
    return {from: schedYmd(new Date(y, m, 1)), to: schedYmd(new Date(y, m + 1, 0))};
  }
  return SCHED.range || schedRange();
}
function schedExportXlsx(){
  var range = schedExportRange();
  var url = '/api/interviews/calendar/export?from=' + encodeURIComponent(range.from) +
    '&to=' + encodeURIComponent(range.to);
  fetch(url, {headers: hdr({})}).then(function(r){
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return r.blob();
  }).then(function(b){
    saveBlob(b, '面试日程-' + range.from + '至' + range.to + '.xlsx');
    toast('已导出 Excel');
  }).catch(function(e){ toast('导出失败：' + e, 'danger'); });
}
function schedSetView(v){
  SCHED.view = v;
  schedLoad();
}
function schedPrev(){
  if(SCHED.view === 'month'){
    SCHED.anchor = new Date(SCHED.anchor.getFullYear(), SCHED.anchor.getMonth() - 1, 1);
  }else SCHED.anchor = schedAddDays(SCHED.anchor, SCHED.view === 'week' ? -7 : -1);
  schedLoad();
}
function schedNext(){
  if(SCHED.view === 'month'){
    SCHED.anchor = new Date(SCHED.anchor.getFullYear(), SCHED.anchor.getMonth() + 1, 1);
  }else SCHED.anchor = schedAddDays(SCHED.anchor, SCHED.view === 'week' ? 7 : 1);
  schedLoad();
}
function schedToday(){ SCHED.anchor = new Date(); schedLoad(); }
function schedOpenDay(day){
  var d = schedDateOf(day);
  if(!d) return;
  SCHED.anchor = d;
  SCHED.view = 'day';
  schedLoad();
}
function schedOpenCandidate(cid){
  if(!cid) return;
  setView('pool');
  pick(Number(cid));
}
function schedFindEvent(aid){
  return (SCHED.events || []).filter(function(e){
    return String(e.application_id) === String(aid);
  })[0] || null;
}
async function schedMoveEvent(ev, date, time){
  var body = {date: date};
  if(time !== undefined) body.time = time;
  var r = await api('/api/interviews/' + ev.application_id + '/reschedule', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  if(r.__err){ toast('调整失败：' + r.__err, 'danger'); return; }
  toast(r.message || '已调整面试时间');
  await schedLoad();       /* 以服务端结果为准，避免本地时间和库里的值分叉 */
}
function schedBindDrag(el, ev){
  el.addEventListener('dragstart', function(e){
    SCHED_DRAG = ev;
    SCHED_DRAG_JUST = true;
    el.classList.add('dragging');
    try{ e.dataTransfer.setData('text/plain', String(ev.application_id)); }catch(_e){}
    if(e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
  });
  el.addEventListener('dragend', function(){
    el.classList.remove('dragging');
    SCHED_DRAG = null;
    setTimeout(function(){ SCHED_DRAG_JUST = false; }, 120);
  });
  el.addEventListener('click', function(e){
    if(SCHED_DRAG_JUST){ e.preventDefault(); e.stopPropagation(); return; }
    schedOpenCandidate(ev.candidate_id);
  });
}
function schedDropTarget(el, getTime){
  el.addEventListener('dragover', function(e){
    if(!SCHED_DRAG) return;
    e.preventDefault();
    if(e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    el.classList.add('drag-over');
  });
  el.addEventListener('dragleave', function(e){
    if(!el.contains(e.relatedTarget)) el.classList.remove('drag-over');
  });
  el.addEventListener('drop', async function(e){
    if(!SCHED_DRAG) return;
    e.preventDefault();
    el.classList.remove('drag-over');
    var ev = SCHED_DRAG, date = el.getAttribute('data-date');
    var time = getTime ? getTime(e, el) : undefined;
    SCHED_DRAG = null;
    if(!date) return;
    await schedMoveEvent(ev, date, time);
  });
}
function bindSchedule(box){
  Array.prototype.forEach.call(box.querySelectorAll('.sched-ev[data-aid]'), function(el){
    var ev = schedFindEvent(el.getAttribute('data-aid'));
    if(ev) schedBindDrag(el, ev);
  });
  Array.prototype.forEach.call(box.querySelectorAll('.sched-more[data-more]'), function(el){
    el.addEventListener('click', function(){ schedOpenDay(el.getAttribute('data-more')); });
  });
  Array.prototype.forEach.call(box.querySelectorAll('.sched-cell[data-date]'), function(el){
    el.addEventListener('click', function(e){
      if(SCHED_DRAG_JUST) return;
      if(e.target.closest('.sched-ev, .sched-more')) return;
      schedOpenDay(el.getAttribute('data-date'));
    });
    schedDropTarget(el, null);
  });
  Array.prototype.forEach.call(box.querySelectorAll('.sched-daycol[data-date]'), function(el){
    schedDropTarget(el, function(e, col){
      var hours = col.querySelector('.sched-hours');
      if(!hours) return;
      var r = hours.getBoundingClientRect();
      if(e.clientY < r.top) return '';          /* 拖到“全天”区：清掉具体时刻 */
      var H0 = 8, H1 = 18, HOUR = SCHED.hour || 46;
      var mins = H0 * 60 + Math.round((e.clientY - r.top) / HOUR * 60 / 15) * 15;
      mins = Math.max(H0 * 60, Math.min(H1 * 60 - 15, mins));
      return schedPad(Math.floor(mins / 60)) + ':' + schedPad(mins % 60);
    });
  });
}

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
    var lc = String(lang || '').toLowerCase();
    /* Mermaid 图表直接生成 SVG；解析失败才退回普通代码块 */
    if(lc === 'mermaid' || lc === 'pie' || lc === 'xychart' ||
       lc === 'xychart-beta' || lc === ''){
      var svg = mermaidChartSvg(code);
      if(svg){
        blocks.push(svg);
        return '\n<pre>ZBLOCK' + (blocks.length - 1) + 'Z</pre>\n';
      }
    }
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
      var html = '<div class="mdtable-wrap">' +
        '<div class="mdtable-bar">' +
        '<button class="mdt-btn" data-mdtable-copy>复制表格</button>' +
        '<button class="mdt-btn" data-mdtable-csv>下载 CSV</button>' +
        '</div><table class="mdtable"><thead><tr>'
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
  s = s.replace(/<pre>\s*ZBLOCK(\d+)Z?\s*<\/pre>/g, function(m, i){ return blocks[+i]; });
  s = s.replace(/ZBLOCK(\d+)Z/g, function(m, i){ return blocks[+i]; });
  return s;
}
/* ---------- 对话里的表格 / 图表：复制、下载 ----------
   模型给的表格以前只能看；HR 要拿去汇报时得自己选中复制。
   这里用事件委托，所有 mdLite 生成的表格/图表都自动带上按钮。 */
function csvCell(v){
  v = String(v == null ? '' : v);
  return '"' + v.replace(/"/g, '""') + '"';
}
function downloadCsv(rows, name){
  var txt = '﻿' + rows.map(function(r){
    return r.map(csvCell).join(',');
  }).join('\r\n');
  saveBlob(new Blob([txt], {type:'text/csv;charset=utf-8'}), name || '表格.csv');
}
function mdTableRows(table){
  var out = [];
  Array.prototype.forEach.call(table.querySelectorAll('tr'), function(tr){
    var cells = [];
    Array.prototype.forEach.call(tr.querySelectorAll('th,td'), function(td){
      cells.push(String(td.innerText || td.textContent || '').replace(/\s+/g, ' ').trim());
    });
    if(cells.length) out.push(cells);
  });
  return out;
}
document.addEventListener('click', function(e){
  var t = e.target;
  if(!t || !t.closest) return;
  var retry = t.closest('[data-retry]');
  if(retry){ retryAgent(retry); return; }
  var edit = t.closest('[data-edit-msg]');
  if(edit){ editUserMessage(edit); return; }
  var dl = t.closest('[data-out-dl]');
  if(dl){
    var _url = dl.getAttribute('data-out-dl');
    var _name = dl.getAttribute('data-out-name') || '下载文件';
    fetch(_url, {headers: hdr({})}).then(function(r){
      if(!r.ok) throw new Error('HTTP ' + r.status);
      return r.blob();
    }).then(function(b){ saveBlob(b, _name); toast('已下载'); })
      .catch(function(e){ toast('下载失败：' + e, 'danger'); });
    return;
  }
  var btn = t.closest('[data-mdtable-copy],[data-mdtable-csv],[data-chart-svg]');
  if(!btn) return;
  var wrap = btn.closest('.mdtable-wrap');
  if(wrap){
    var table = wrap.querySelector('table');
    if(!table) return;
    var rows = mdTableRows(table);
    if(btn.hasAttribute('data-mdtable-copy')){
      copyText(rows.map(function(r){ return r.join('\t'); }).join('\n'));
    }else{
      downloadCsv(rows, '助手表格.csv');
    }
    return;
  }
  var chart = btn.closest('.mchart');
  if(chart){
    var svg = chart.querySelector('svg');
    if(svg){
      saveBlob(new Blob([svg.outerHTML], {type:'image/svg+xml;charset=utf-8'}), '助手图表.svg');
    }
  }
});

/* ---------- 快捷问题：**可配置**（增删改，存本地） ----------
   每个人常用的问题不一样（HR 问的、主管问的、面试官问的都不一样），
   写死一组对谁都不合适 —— 让人自己配。 */
var CHAT_ASKS = null;
function chatAsksLoad(){
  if(CHAT_ASKS) return CHAT_ASKS;
  var defaults = [
    '2027届招聘进度怎么样？还差多少人？',
    '哪些单位缺口最大？',
    '这届面试了谁？谁还没签约？',
    '人才库里 A 档有多少人？',
    '有哪些人还缺联系方式？',
    '给我一份面试提纲'
  ];
  try{
    var raw = localStorage.getItem('chat_welcome_asks');
    CHAT_ASKS = raw ? JSON.parse(raw) : defaults;
  }catch(e){
    CHAT_ASKS = defaults;
  }
  if(!CHAT_ASKS.length) CHAT_ASKS = defaults;
  return CHAT_ASKS;
}
function chatAsksSave(list){
  CHAT_ASKS = list;
  try{ localStorage.setItem('chat_welcome_asks', JSON.stringify(list)); }catch(e){}
  /* v4.0.17：这组问题现在配置的是**欢迎页**，右侧不再有“可以这样问”。
     保存后如果欢迎页正开着，立刻重画。 */
  if(document.getElementById('chatWelcome')) renderWelcome();
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
  openProgDlg('配置欢迎问题',
    rows +
    '<div class="crow" style="margin-top:8px"><label>新增</label>' +
      '<div class="cinput"><input class="ced" id="chatAskNew" placeholder="写一条欢迎页问题">' +
      '<button class="btn sm" onclick="chatAskAdd()">添加</button></div></div>' +
    '<div class="hint" style="margin-top:8px">这些问题会显示在智能助手欢迎页；' +
      '改完点「保存」生效，只存在你自己的浏览器里。</div>' +
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
  toast('已保存 ' + list.length + ' 条欢迎问题');
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
      '<div class="chatconv">' +
        '<div class="chatconv-head"><h3>对话</h3>' +
          '<button class="btn sm" id="chatNew">新对话</button></div>' +
        '<div class="chatconv-list" id="chatConvList"></div>' +
      '</div>' +
      '<div class="chatwrap">' +
        '<div class="chatlog" id="chatLog"></div>' +
        '<div class="chatinput"><div class="ciwrap">' +
          '<textarea id="chatIn" rows="3" placeholder="输入指令或问题，Enter 发送"></textarea>' +
          '<button class="btn primary" id="chatSend" aria-label="发送" title="发送">' +
            '<svg class="ci-ico ci-enter" viewBox="0 0 24 24" aria-hidden="true">' +
              '<path d="M20 5v7a3 3 0 0 1-3 3H6"></path>' +
              '<path d="m10 11-4 4 4 4"></path></svg>' +
            '<svg class="ci-ico ci-stop" viewBox="0 0 24 24" aria-hidden="true">' +
              '<rect x="7.5" y="7.5" width="9" height="9" rx="2"></rect></svg>' +
          '</button>' +
        '</div></div>' +
      '</div>' +
      '<div class="chatside">' +
        '<div class="sec chat-side-metrics" id="chatMetricsSec" style="margin-top:0">' +
          '<h3 class="askhead">运行指标' +
          '<span class="hint" id="metricState">近 7 天</span></h3>' +
          '<div id="chatMetrics" class="metricpanel">' +
            '<div class="hint">读取中…</div></div></div>' +
        '<div class="sec chat-side-context" id="chatContextSec">' +
          '<h3 class="askhead">上下文' +
          '<span class="hint" id="ctxState"></span></h3>' +
          '<div id="chatContext" class="ctxpanel">' +
            '<div class="hint">提问后显示</div></div></div>' +
        '<div class="sec chat-side-outputs" id="chatOutputsSec">' +
          '<h3 class="askhead">本次产出' +
          '<span class="hint" id="outState">—</span></h3>' +
          '<div id="chatOutputs" class="outpanel">' +
            '<div class="hint">回答后显示下载 / 草稿 / 提案</div></div></div>' +
        '<div class="sec chat-side-trace" id="chatTraceSec">' +
          '<h3 class="askhead">调用轨迹' +
          '<span class="hint" id="traceState">等待提问</span></h3>' +
          '<div id="chatTrace" class="tracepanel"></div></div>' +
      '</div>';
    var inp = document.getElementById('chatIn');
    inp.addEventListener('keydown', function(ev){
      if(ev.key === 'Enter' && !ev.shiftKey){ ev.preventDefault(); askAgent(); }
    });
    inp.addEventListener('input', function(){ chatGrow(inp); });
    setTimeout(function(){ chatGrow(inp); }, 0);
    document.getElementById('chatSend').addEventListener('click', function(){
      if(CHAT_CTRL) stopRunningChat();
      else askAgent();
    });
    document.getElementById('chatNew').addEventListener('click', newChat);
    renderTrace(null);
    loadChatMetrics();
    loadChatContext();
    renderOutputs(null, true);
    syncChatSidebar();
  }
  if(!CHAT_BOOTED){
    CHAT_BOOTED = true;
    if(!CHAT_CONV_ID) newChat();
  }
  await loadConversations();
  killAutofill();
}

function newConvId(){
  return 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

/* 切换/新建对话时，**不结束**正在生成的那一轮：请求继续在后台跑，
   完成后由服务端按原 session_id 入库；切回来时历史里就能看到。
   CHAT_GEN 是「界面世代号」：切换时 +1，旧请求的 onEvent / 收尾回调
   发现世代号对不上就不再碰新对话的 DOM，但也不再 abort。 */
var CHAT_GEN = 0;
function leaveRunningChat(){
  var was = !!CHAT_CTRL;
  CHAT_GEN += 1;
  stopTracePoll();
  /* 不 abort、不清 CHAT_CTRL/CHAT_STREAMING：后台继续生成。
     发送按钮保持“停止”态，用户随时可以真正中止这一轮。 */
  return was;
}

async function loadConversations(){
  var r = await api('/api/agent/conversations');
  if(r && !r.__err){
    CHAT_CONVS = r.items || [];
    renderConvList();
  }
}

/* 发送即入列：服务端要等一轮跑完才写会话行，左侧列表不能因此空着。
   先放一条本地临时会话（标题=这句提问），回答落库后 loadConversations 会替换。 */
function ensureConvInList(cid, q){
  if(!cid) return;
  var list = CHAT_CONVS || [];
  if(list.some(function(c){ return c.id === cid; })) return;
  var d = new Date();
  var p = function(n){ return (n < 10 ? '0' : '') + n; };
  var stamp = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  var title = String(q || '新对话').replace(/\s+/g, ' ').trim().slice(0, 48) || '新对话';
  list.unshift({id: cid, title: title, message_count: 0,
                updated_at: stamp, pending: true});
  CHAT_CONVS = list;
  renderConvList();
}
function renderConvList(){
  var box = document.getElementById('chatConvList');
  if(!box) return;
  /* restoreView 在脚本前部就会进入 initChat → newChat，而 CHAT_CONVS 的 var
     赋值在脚本后部；此处必须容错，否则首屏直接抛 undefined.length，左侧会话列表不再加载。 */
  var convs = CHAT_CONVS || [];
  if(!convs.length){
    box.innerHTML = '<div class="chatconv-empty">还没有对话<br>点「新对话」开始</div>';
    return;
  }
  box.innerHTML = convs.map(function(c){
    var active = (c.id === CHAT_CONV_ID) ? ' on' : '';
    var title = c.title || '新对话';
    var live = !!(CHAT_CTRL && RUN_CONV_ID === c.id);
    var meta = live
      ? '<span class="cc-live">生成中…</span>'
      : ((c.message_count || 0) + ' 轮 · ' + esc(String(c.updated_at || '').slice(5, 16)));
    return '<div class="chatconv-item' + active + '" onclick="openConversation(\'' +
        esc(c.id) + '\')">' +
      '<div class="cc-title">' + esc(title) + '</div>' +
      '<div class="cc-meta">' + meta + '</div>' +
      '<button class="cc-ren" title="重命名" onclick="event.stopPropagation();renameConversation(\'' +
        esc(c.id) + '\')">✎</button>' +
      '<button class="cc-del" title="删除会话" onclick="event.stopPropagation();deleteConversation(\'' +
        esc(c.id) + '\')">×</button></div>';
  }).join('');
}

/* 今日建议点过就开始处理了：当天不再重复出现。
   只存标题，不存数据；第二天自动失效。 */
function briefDateKey(){
  var d = new Date();
  var m = String(d.getMonth() + 1), day = String(d.getDate());
  if(m.length < 2) m = '0' + m;
  if(day.length < 2) day = '0' + day;
  return d.getFullYear() + '-' + m + '-' + day;
}
function briefDismissedMap(){
  try{
    var raw = localStorage.getItem('chat_brief_dismissed');
    if(!raw) return {};
    var o = JSON.parse(raw);
    if(!o || o.date !== briefDateKey()) return {};
    var map = {};
    (o.keys || []).forEach(function(k){ map[k] = 1; });
    return map;
  }catch(e){ return {}; }
}
function dismissBrief(title){
  title = String(title || '').trim();
  if(!title) return;
  try{
    var map = briefDismissedMap();
    map[title] = 1;
    localStorage.setItem('chat_brief_dismissed',
      JSON.stringify({date: briefDateKey(), keys: Object.keys(map)}));
  }catch(e){}
}
function renderWelcome(){
  var log = document.getElementById('chatLog');
  if(!log) return;
  var asks = chatAsksLoad();
  log.innerHTML = '<div class="chatwelcome" id="chatWelcome">' +
    '<h2>今天想先处理哪件事？</h2>' +
    '<p>查人、推进流程、汇总招聘进度都可以直接说。写操作会先停在「待确认提案」，你确认后才生效。</p>' +
    '<div class="cw-asks">' + asks.map(function(q){
      return '<button class="cw-card" data-q="' + esc(q) + '">' +
        '<span class="cw-q">' + esc(q) + '</span></button>';
    }).join('') + '</div>' +
    '<div class="cw-config"><button class="btn sm" onclick="chatAsksConfig()">' +
      '配置这些问题</button></div></div>';
  Array.prototype.forEach.call(log.querySelectorAll('.cw-card'), function(b){
    b.addEventListener('click', function(){
      var inp = document.getElementById('chatIn');
      if(inp){ inp.value = b.dataset.q; askAgent(); }
    });
  });
  /* 主动建议：复用概览页同一份「今日待办」（规则版或模型判断版）。
     没有待办就不显示，避免把一个空区块挂在欢迎页上。 */
  api('/api/brief/today').then(function(br){
    var box = document.getElementById('chatWelcome');
    if(!box || box.getAttribute('data-brief') === '1') return;
    if(!br || br.__err || !(br.priorities || []).length) return;
    box.setAttribute('data-brief', '1');
    var _dismissed = briefDismissedMap();
    var items = (br.priorities || []).filter(function(p){
      return !_dismissed[String(p.title || '')];
    }).slice(0, 3);
    if(!items.length) return;
    var html = '<div class="cw-brief"><div class="cw-brief-h">今日建议' +
      (br.source === 'rule' ? ' · 规则排序' : '') + '</div>' +
      items.map(function(p){
        var q = '帮我处理：' + (p.title || '') +
          (p.why ? '（' + p.why + '）' : '');
        return '<button class="cw-card cw-brief-card" data-q="' + esc(q) +
          '" data-brief-title="' + esc(p.title || '') + '">' +
          '<span class="cw-q">' + esc(p.title || '') + '</span>' +
          (p.why ? '<span class="cw-w">' + esc(p.why) + '</span>' : '') +
          '</button>';
      }).join('') + '</div>';
    box.insertAdjacentHTML('beforeend', html);
    Array.prototype.forEach.call(box.querySelectorAll('.cw-brief-card'), function(b){
      b.addEventListener('click', function(){
        var inp = document.getElementById('chatIn');
        if(!inp || CHAT_CTRL) return;
        dismissBrief(b.dataset.briefTitle || '');
        inp.value = b.dataset.q;
        askAgent();
        /* askAgent 在第一个 await 前已同步设置 CHAT_CTRL。
           确认真的开始运行后，把**这一条**待办从快捷区移除；
           如果整个欢迎页已被 pushMsg 移除，这段自然是空操作。 */
        setTimeout(function(){
          if(!CHAT_CTRL) return;
          var sec = b.closest('.cw-brief');
          b.remove();
          if(sec && !sec.querySelector('.cw-brief-card')) sec.remove();
        }, 0);
      });
    });
  });
}

var CHAT_TRACE_TIMER = null;

function thinkingHtml(text, open, seconds){
  text = String(text || '').trim();
  if(!text) return '';
  var sec = Number(seconds || 0);
  var when = sec > 0 ? '（用时 ' + Math.max(1, Math.round(sec)) + ' 秒）' : '';
  return '<details class="thinkbox"' + (open ? ' open' : '') + '>' +
    '<summary><span class="think-label">已思考' + when + '</span></summary>' +
    '<div class="think-body">' + mdLite(text) + '</div></details>';
}
/* 等待模型时把已经拿到的思维链实时显示出来；答复落地后整体换成折叠块。 */
function renderThinkingLive(holder, text){
  text = String(text || '').trim();
  if(!holder || !text) return;
  var body = holder.querySelector('.think-live .think-body');
  if(!body){
    holder.innerHTML = '<div class="think-live"><div class="think-live-h">正在思考</div>' +
      '<div class="think-body"></div></div><div class="hint">正在生成回答…</div>';
    body = holder.querySelector('.think-live .think-body');
  }
  if(body && body.getAttribute('data-n') !== String(text.length)){
    body.setAttribute('data-n', String(text.length));
    body.innerHTML = mdLite(text);
    body.scrollTop = body.scrollHeight;   /* 思考在增长，视线跟着走 */
  }
}
/* 连续同名工具合并：模型批量读 5 个人时，界面上只占一行
   “读取档案 ×5（孙跃跃、贾业婷…）”，不再刷 5 行。 */
function groupTrace(trace){
  var out = [];
  (trace || []).forEach(function(t){
    var last = out[out.length - 1];
    if(last && last.tool === t.tool){
      last.items.push(t);
      last.count = last.items.length;
      last.write = last.write || !!t.write;
      last.disabled = last.disabled || !!t.disabled;
      last.recovered = last.recovered || !!t.recovered;
    } else {
      out.push({tool: t.tool, tool_label: t.tool_label || t.tool,
                items: [t], count: 1,
                write: !!t.write, disabled: !!t.disabled, recovered: !!t.recovered});
    }
  });
  return out;
}
function traceArgText(t){
  var a = t.args || {};
  var keys = ['candidate_id', 'name', 'job', 'keyword', 'stage', 'tier',
              'application_id', 'education', 'unit', 'query', 'similar_to',
              'skills', 'view', 'outcome', 'date_from', 'date_to'];
  var vals = [];
  keys.forEach(function(k){
    var v = a[k];
    if(v === null || v === undefined || v === '') return;
    if(Array.isArray(v)) vals = vals.concat(v);
    else vals.push(v);
  });
  var seen = {}, out = [];
  vals.forEach(function(v){
    var s = String(v);
    if(s && !seen[s]){ seen[s] = 1; out.push(s); }
  });
  return out.slice(0, 8).join('、') + (out.length > 8 ? '…' : '');
}
function traceGroupMeta(g){
  var seen = {}, out = [];
  (g.items || []).forEach(function(t){
    var s = traceArgText(t);
    if(!s) return;
    s.split('、').forEach(function(x){
      x = x.replace(/…$/, '');
      if(x && !seen[x]){ seen[x] = 1; out.push(x); }
    });
  });
  return out.slice(0, 10).join('、') + (out.length > 10 ? '…' : '');
}
function traceStepHtml(dot, title, meta, badges){
  return '<div class="tp-step"><span class="tp-dot ' + dot + '"></span>' +
    '<div class="tp-box"><div class="tp-title">' + esc(title) + '</div>' +
    (meta ? '<div class="tp-meta">' + esc(meta) + '</div>' : '') +
    (badges || '') + '</div></div>';
}
function traceToolStep(t){
  var count = t.count || 1;
  var label = t.tool_label || t.tool || '工具';
  var meta;
  if(count > 1){
    label += ' ×' + count;
    meta = traceGroupMeta(t);
  } else {
    var args = t.args || {};
    meta = Object.keys(args).length ? JSON.stringify(args) : '';
  }
  if(meta.length > 140) meta = meta.slice(0, 140) + '…';
  var badges = '';
  if(t.write) badges += '<span class="tp-badge write">写 · 待确认</span>';
  if(t.disabled) badges += '<span class="tp-badge disabled">已拦截</span>';
  return traceStepHtml('t', label, meta, badges);
}
async function loadChatMetrics(){
  var box = document.getElementById('chatMetrics');
  if(!box) return;
  var m = await api('/api/agent/metrics?days=7');
  if(!m || m.__err){
    box.innerHTML = '<div class="hint">指标读取失败</div>';
    return;
  }
  function fmt(ms){
    if(!ms) return '—';
    return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms';
  }
  var costRow = '';
  if(m.cost !== null && m.cost !== undefined){
    costRow = '<div class="metric-row"><span class="metric-k">成本</span>' +
      '<span class="metric-v">' + esc((m.currency || '¥') + ' ' + m.cost) + '</span></div>';
  }
  box.innerHTML =
    '<div class="metric-row"><span class="metric-k">请求</span>' +
      '<span class="metric-v">' + (m.runs || 0) + ' 次 · 成功 ' + (m.ok_rate || 0) + '%</span></div>' +
    '<div class="metric-row"><span class="metric-k">延迟</span>' +
      '<span class="metric-v">P50 ' + fmt(m.latency_p50) + ' · P95 ' + fmt(m.latency_p95) + '</span></div>' +
    '<div class="metric-row"><span class="metric-k">今日 Token</span>' +
      '<span class="metric-v">' + fmtTokens(m.today_tokens || 0) + '</span></div>' +
    '<div class="metric-row" title="服务商需在 usage 中返回 cached_tokens / prompt_cache_hit_tokens 才能计算">' +
      '<span class="metric-k">缓存命中</span>' +
      '<span class="metric-v">' +
        (m.today_cache_rate === null || m.today_cache_rate === undefined
          ? '未上报' : m.today_cache_rate + '%') +
      '</span></div>' +
    '<div class="metric-row" title="工具结果占输入 token 的比例；错误率=工具报错次数/工具调用次数">' +
      '<span class="metric-k">工具质量</span>' +
      '<span class="metric-v">' +
        (m.today_tool_ratio === null || m.today_tool_ratio === undefined
          ? '占比 —' : '占比 ' + m.today_tool_ratio + '%') +
        ' · 错误 ' +
        (m.today_tool_error_rate === null || m.today_tool_error_rate === undefined
          ? '—' : m.today_tool_error_rate + '%') +
      '</span></div>' +
    '<div class="metric-row" title="上下文接近/超过预算、工具占比过高、工具错误率过高、轮次用尽等">' +
      '<span class="metric-k">上下文告警</span>' +
      '<span class="metric-v">' +
        (m.alert_runs ? (m.high_alert_runs || 0) + ' 高 / ' + m.alert_runs + ' 次' : '无') +
      '</span></div>' +
    costRow;
}
async function loadChatContext(){
  if(CHAT_CTRL) return;                     /* 正在回答时以实际上下文为准 */
  var q = CHAT_CONV_ID ? ('?session_id=' + encodeURIComponent(CHAT_CONV_ID)) : '';
  var c = await api('/api/agent/context' + q);
  if(c && !c.__err) renderContext(c);
}
function fmtTokens(n){
  n = Number(n) || 0;
  if(n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
  if(n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(n);
}
function renderContext(c){
  var box = document.getElementById('chatContext');
  if(!box) return;
  var _st = document.getElementById('ctxState');
  if(_st) _st.title = '窗口优先取模型端点 /models 的 context_length；取不到时用 config/model.json 的 context_window；再取不到按 1M 默认。token 为估算值。';
  box.title = '窗口优先取模型端点 /models 的 context_length；取不到时用 config/model.json 的 context_window；再取不到按 1M 默认。参数量取不到时显示 —，可在 config/model.json 的 parameter_count 指定。';
  if(!c || !c.window){
    box.innerHTML = '<div class="hint">提问后显示</div>';
    return;
  }
  var colors = {messages:'#4E79A7', system_tools:'#E15759',
                system_prompt:'#59A14F', skills:'#C56A45',
                memory:'#B07AA1', free:'#e2e2e2'};
  var parts = c.parts || [];
  var segs = parts.map(function(p){
    var pct = Math.max(0, Math.min(100, (Number(p.tokens) || 0) / c.window * 100));
    return '<div class="ctxseg" style="width:' + pct + '%;background:' +
      (colors[p.key] || '#ccc') + '" title="' + esc(p.label) + '"></div>';
  }).join('');
  var rows = parts.map(function(p){
    var pct = (Number(p.tokens) || 0) / c.window * 100;
    return '<div class="ctxrow"><span class="ctxdot" style="background:' +
      (colors[p.key] || '#ccc') + '"></span><span class="ctxname">' +
      esc(p.label) + '</span><span class="ctxnum">' + fmtTokens(p.tokens) +
      ' · ' + pct.toFixed(1) + '%</span></div>';
  }).join('');
  var gov = c.governance || {};
  var trimmed = (c.context_trimmed === undefined ? gov.trimmed_tokens : c.context_trimmed);
  if(gov.estimated_before || trimmed){
    rows += '<div class="ctxrow"><span class="ctxname">上下文治理</span>' +
      '<span class="ctxnum">压缩 ' + fmtTokens(trimmed || 0) +
      ' · 工具占比 ' + ((Number(c.tool_ratio === undefined ? gov.tool_ratio : c.tool_ratio) || 0) * 100).toFixed(0) +
      '%</span></div>';
  }
  var alerts = c.alerts || [];
  if(alerts.length){
    rows += '<div class="ctxrow"><span class="ctxname" style="color:#b25000">告警</span>' +
      '<span class="ctxnum" style="color:#b25000">' + esc(alerts[0].message || (alerts.length + ' 项')) +
      '</span></div>';
  }
  var modelLine = c.model
    ? '<div class="ctxmodel"><b>' + esc(c.model) + '</b></div>'
    : '';
  box.innerHTML = modelLine + '<div class="ctxhead"><b>' +
    (Number(c.percent) || 0).toFixed(1) + '% 已用</b><span>' +
    fmtTokens(c.used) + ' / ' + fmtTokens(c.window) + '</span></div>' +
    '<div class="ctxbar">' + segs + '</div>' + rows;
}
function collectOutputs(r){
  var out = [], seen = {};
  (r.trace || []).forEach(function(t){
    var d = t.download || null;
    if(!d){
      try{ d = JSON.parse(t.result || '{}'); }catch(e){ return; }
    }
    if(!d || !d.download_url || seen[d.download_url]) return;
    seen[d.download_url] = 1;
    var url = d.download_url;
    var ext = url.indexOf('interview-schedule') >= 0 || url.indexOf('export/roster') >= 0
      ? '.xlsx' : (url.indexOf('bundle') >= 0 ? '.zip' : '');
    out.push({kind:'file', icon:'↓', tag:'下载',
              label:(d.scope || t.tool_label || '导出文件') + '-' + todayStamp() + ext,
              url:url});
  });
  /* 历史回放：下载链接存在 agent_runs.tool_calls[].download 里 */
  (r.tools || []).forEach(function(t){
    var dl = t.download;
    if(!dl && t.download_url){
      dl = {download_url: t.download_url, scope: t.scope || ''};
    }
    if(!dl || !dl.download_url || seen[dl.download_url]) return;
    seen[dl.download_url] = 1;
    var url = dl.download_url;
    var ext = url.indexOf('interview-schedule') >= 0 || url.indexOf('export/roster') >= 0
      ? '.xlsx' : (url.indexOf('bundle') >= 0 ? '.zip' : '');
    out.push({kind:'file', icon:'↓', tag:'下载',
              label:(dl.scope || t.tool_label || '导出文件') + '-' + todayStamp() + ext,
              url:url});
  });
  (r.downloads || []).forEach(function(dl){
    if(!dl || !dl.download_url || seen[dl.download_url]) return;
    seen[dl.download_url] = 1;
    var url = dl.download_url;
    var ext = url.indexOf('interview-schedule') >= 0 || url.indexOf('export/roster') >= 0
      ? '.xlsx' : (url.indexOf('bundle') >= 0 ? '.zip' : '');
    out.push({kind:'file', icon:'↓', tag:'下载',
              label:(dl.scope || '导出文件') + '-' + todayStamp() + ext,
              url:url});
  });
  (r.drafts || []).forEach(function(d){
    out.push({kind:'draft', icon:'✉', tag:'草稿',
              label:(d.subject || d.name || '邮件草稿')});
  });
  /* 待确认提案不进右侧「本次产出」：它是待办，不是这一轮生成的产物。
     提案本身在对话卡片和「待确认提案」面板里处理，避免同一件事出现两遍。 */
  return out;
}
function outputKey(x){
  return x.kind + '|' + (x.url || x.label || '');
}
function mergeOutputs(items){
  var seen = {};
  CHAT_OUTPUTS.forEach(function(x){ seen[outputKey(x)] = 1; });
  (items || []).forEach(function(x){
    var k = outputKey(x);
    if(!seen[k]){ seen[k] = 1; CHAT_OUTPUTS.push(x); }
  });
}
function renderOutputs(r, reset){
  var box = document.getElementById('chatOutputs');
  if(!box) return;
  if(reset) CHAT_OUTPUTS = [];
  if(r) mergeOutputs(collectOutputs(r));
  /* 过滤掉旧版本已经收进来的 proposal 项，避免升级后旧会话仍显示提案。 */
  var items = CHAT_OUTPUTS.filter(function(x){ return x.kind !== 'proposal'; });
  var st = document.getElementById('outState');
  if(st) st.textContent = items.length ? (items.length + ' 项') : '—';
  if(!items.length){
    box.innerHTML = '<div class="hint">当前对话还没有下载 / 草稿</div>';
    return;
  }
  box.innerHTML = items.map(function(x){
    if(x.kind === 'file'){
      return '<button class="outitem" data-out-dl="' + esc(x.url) +
        '" data-out-name="' + esc(x.label) + '"><span class="outicon">↓</span>' +
        '<span class="outname">' + esc(x.label) + '</span>' +
        '<span class="outtag">' + esc(x.tag) + '</span></button>';
    }
    return '<div class="outitem" style="cursor:default"><span class="outicon">' +
      esc(x.icon) + '</span><span class="outname">' + esc(x.label) + '</span>' +
      '<span class="outtag">' + esc(x.tag) + '</span></div>';
  }).join('');
}
function renderTrace(r, question){
  var box = document.getElementById('chatTrace');
  if(!box) return;
  var state = document.getElementById('traceState');
  if(!r){
    if(state) state.textContent = '等待提问';
    box.innerHTML = '<div class="trace-empty">提问后这里会显示模型与工具调用链。</div>';
    return;
  }
  if(state){
    state.textContent = r.mode === 'rule' ? '规则模式'
      : (r.mode === 'offline' ? '离线模式'
        : (r.mode === 'simple' ? '快捷回复' : '模型模式'));
  }
  var steps = [];
  var modeText = r.mode === 'rule' ? '规则路由（模型未就绪）'
    : (r.mode === 'offline' ? '离线规则'
      : (r.mode === 'simple' ? '快捷回复（未调模型）' : 'LLM'));
  steps.push(traceStepHtml('m', '模型', modeText +
    (r.rounds ? ' · ' + r.rounds + ' 轮' : '') +
    (r.latency_ms ? ' · ' + r.latency_ms + ' ms' : '')));
  groupTrace(r.trace || []).forEach(function(t){ steps.push(traceToolStep(t)); });
  steps.push(traceStepHtml('a', '生成回答', question ? ('问题：' + question) : ''));
  box.innerHTML = steps.join('');
}
function renderTraceLive(t, question){
  var box = document.getElementById('chatTrace');
  if(!box) return;
  var state = document.getElementById('traceState');
  if(state) state.textContent = '运行中';
  var steps = [traceStepHtml('m', '模型', '正在思考…')];
  groupTrace(t.trace || []).forEach(function(x){ steps.push(traceToolStep(x)); });
  steps.push('<div class="tp-step tp-running"><span class="tp-dot t"></span>' +
    '<div class="tp-box"><div class="tp-title">运行中</div><div class="tp-meta">' +
    esc(question ? ('问题：' + question) : '等待下一步') + '</div></div></div>');
  box.innerHTML = steps.join('');
}
function renderTraceRunning(question){
  var box = document.getElementById('chatTrace');
  if(!box) return;
  var state = document.getElementById('traceState');
  if(state) state.textContent = '运行中';
  box.innerHTML = traceStepHtml('m', '模型', '正在思考…') +
    '<div class="tp-step tp-running"><span class="tp-dot t"></span>' +
    '<div class="tp-box"><div class="tp-title">运行中</div><div class="tp-meta">' +
    esc(question || '') + '</div></div></div>';
}
function startTracePoll(question, holder){
  stopTracePoll();
  CHAT_TRACE_TIMER = setInterval(async function(){
    if(!CHAT_CTRL) return;
    var t = await api('/api/agent/trace?session_id=' + encodeURIComponent(CHAT_CONV_ID));
    if(t && !t.__err){
      // 思考过程实时显示；工具链照旧更新右侧面板
      if(t.thinking && !CHAT_STREAMING) renderThinkingLive(holder, t.thinking);
      if(t.trace && t.trace.length) renderTraceLive(t, question);
    }
  }, 700);
}
function stopTracePoll(){
  if(CHAT_TRACE_TIMER){ clearInterval(CHAT_TRACE_TIMER); CHAT_TRACE_TIMER = null; }
}
function renderTraceFromMessages(hmsgs){
  var last = null;
  (hmsgs || []).forEach(function(m){ if(m.role === 'assistant') last = m; });
  if(!last){ renderTrace(null); return; }
  renderTrace({trace: last.tools || [], mode: last.mode, rounds: last.rounds,
               latency_ms: last.latency_ms});
}

function newChat(){
  var wasBusy = leaveRunningChat();
  CHAT_CONV_ID = newConvId();
  CHAT = [];
  var log = document.getElementById('chatLog');
  if(log) log.innerHTML = '';
  renderWelcome();
  renderTrace(null);
  renderContext(null);
  renderOutputs(null, true);
  loadChatContext();
  syncChatSidebar();
  renderConvList();
  chatBusy(!!CHAT_CTRL);
  var inp = document.getElementById('chatIn');
  if(inp){ inp.value = ''; inp.focus(); chatGrow(inp); }
  if(wasBusy) toast('上一轮回答仍在后台生成', 'info');
}

function renderChatMessages(hmsgs){
  var log = document.getElementById('chatLog');
  if(!log) return;
  log.innerHTML = '';
  CHAT_OUTPUTS = [];
  var lastAsst = null;
  (hmsgs || []).forEach(function(m){
    var node = pushMsg(m.role, m.content || '');
    if(m.role === 'assistant' && m.thinking){
      node.insertAdjacentHTML('afterbegin',
        thinkingHtml(m.thinking, false, m.latency_ms ? m.latency_ms / 1000 : 0));
    }
    if(m.role === 'assistant'){
      lastAsst = m;
      var bits = [];
      if(m.mode) bits.push('模式：' + m.mode);
      if(m.rounds) bits.push('轮数 ' + m.rounds);
      if(m.latency_ms) bits.push(m.latency_ms + ' ms');
      if((m.tools || []).length){
        bits.push('工具：' + m.tools.map(function(x){ return x.tool; }).join(' → '));
      }
      if(bits.length){
        node.insertAdjacentHTML('beforeend',
          '<details class="tracebox"><summary>本次调用详情</summary>' +
          '<div class="trace">' + esc(bits.join(' · ')) + '</div></details>');
      }
    }
    if((m.drafts || []).length) renderDraftCards(node, m.drafts);
    if((m.proposals || []).length) renderProposalCards(node, m.proposals);
    if(m.run_id) node.setAttribute('data-run-id', m.run_id);
    addMsgActions(node, m.role);
  });
  CHAT = (hmsgs || []).map(function(x){ return {role: x.role, content: x.content}; });
  if(!hmsgs || !hmsgs.length) renderWelcome();
  renderTraceFromMessages(hmsgs);
  renderContext(lastAsst && lastAsst.context ? lastAsst.context : null);
  (hmsgs || []).forEach(function(m){
    if(m.role === 'assistant') mergeOutputs(collectOutputs(m));
  });
  renderOutputs(null);
  syncChatSidebar();
  return lastAsst;
}

async function openConversation(cid){
  /* 允许在回答过程中直接切走：这一轮不 abort，继续在后台生成。
     旧回答的收尾回调靠 CHAT_GEN / RUN_CONV_ID 判断是否还该碰界面。 */
  var wasBusy = leaveRunningChat();
  var gen = CHAT_GEN;
  var h = await api('/api/agent/history?conversation_id=' + encodeURIComponent(cid));
  if(gen !== CHAT_GEN) return;             /* 期间又点了别的会话 */
  if(h.__err){ toast('读取会话失败：' + h.__err, 'danger'); return; }
  CHAT_CONV_ID = cid;
  var _last = renderChatMessages(h.messages || []);
  /* 如果切回来的正是仍在生成的那一条：把当前累积的思考/答案重新挂到页面上，
     之后的事件会继续往这个新气泡里写。 */
  if(CHAT_CTRL && RUN_CONV_ID === cid && RUN_STATE){
    pushMsg('user', RUN_Q);
    var ph = pushMsg('assistant', '');
    ph.innerHTML = '<div class="stream-status hint"></div>' +
      (RUN_STATE.liveThink
        ? '<div class="think-live"><div class="think-live-h">正在思考</div>' +
          '<div class="think-body"></div></div>' : '') +
      '<div class="stream-answer"></div>';
    var tb = ph.querySelector('.think-body');
    if(tb) tb.textContent = RUN_STATE.liveThink;
    var ae = ph.querySelector('.stream-answer');
    if(ae) ae.innerHTML = RUN_STATE.liveAns ? mdLite(RUN_STATE.liveAns) : '';
    RUN_HOLDER = ph;
    renderTraceRunning(RUN_Q);
    startTracePoll(RUN_Q, ph);
    chatAutoScroll();
  }
  if(!_last || !_last.context) loadChatContext();
  renderConvList();
  chatBusy(!!CHAT_CTRL);
  var inp = document.getElementById('chatIn');
  if(inp) inp.focus();
  if(wasBusy && RUN_CONV_ID && RUN_CONV_ID !== cid){
    toast('上一轮回答仍在后台生成', 'info');
  }
}

async function renameConversation(cid){
  var cur = (CHAT_CONVS || []).filter(function(c){ return c.id === cid; })[0] || {};
  var name = await askText('重命名会话', cur.title || '', '给这条对话起个名字');
  if(!name || name === true) return;
  var r = await api('/api/agent/conversations/' + encodeURIComponent(cid), {
    method:'PATCH', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:String(name).trim()})
  });
  if(r.__err){ toast('重命名失败：' + r.__err, 'danger'); return; }
  await loadConversations();
}

async function deleteConversation(cid){
  if(!cid) return;
  var ok = await askConfirm({
    title: '删除会话', danger: true, okText: '删除',
    body: '从左侧列表删除这条对话？底层运行记录会保留给成本与审计。'
  });
  if(!ok) return;
  var r = await api('/api/agent/conversations/' + encodeURIComponent(cid),
                    {method:'DELETE'});
  if(r.__err){ toast('删除失败：' + r.__err, 'danger'); return; }
  toast('已删除');
  if(cid === CHAT_CONV_ID) newChat();
  await loadConversations();
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
/* 首页（还没有对话）只显示运行指标；进入对话后隐藏指标，
   显示上下文 / 本次产出 / 调用轨迹。 */
function syncChatSidebar(){
  var log = document.getElementById('chatLog');
  var has = !!(log && log.querySelector('.msg'));
  var metrics = document.getElementById('chatMetricsSec');
  if(metrics) metrics.style.display = has ? 'none' : '';
  ['chatContextSec', 'chatOutputsSec', 'chatTraceSec'].forEach(function(id){
    var el = document.getElementById(id);
    if(el) el.style.display = has ? '' : 'none';
  });
}
function pushMsg(role, content){
  var log = document.getElementById('chatLog');
  var welcome = document.getElementById('chatWelcome');
  if(welcome) welcome.remove();
  var el = document.createElement('div');
  el.className = 'msg ' + role;
  el.innerHTML = role === 'assistant' ? mdLite(content) : esc(content);
  /* 用户原话单独存一份：编辑重发时不受气泡里追加的按钮文字影响。 */
  if(role === 'user') el.setAttribute('data-q', content);
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  syncChatSidebar();
  return el;
}
/* 模型一次调用可能要几十秒 —— 必须能**中断**，否则 HR 只能干等或刷新页面。
   用 AbortController 挂在 fetch 上：点「停止」直接 abort，界面立刻回到可输入状态。 */
var CHAT_CTRL = null;
/* 正在生成的那一轮属于哪条会话。切走时回答留在后台跑，
   回到原会话时用它给出「生成中」占位。 */
var RUN_CONV_ID = null;
var RUN_Q = '';
var RUN_HOLDER = null;   /* 当前这一轮答案挂在哪个 DOM 节点（切回来会换绑） */
var RUN_STATE = null;    /* {liveThink, liveAns, statusBase}，供重新挂载时还原 */
/* SSE 正在逐字推送时，右侧轨迹轮询不要再改写同一条消息里的思考块 */
var CHAT_STREAMING = false;
function chatBusy(on){
  var send = document.getElementById('chatSend');
  var ta = document.getElementById('chatIn');
  if(send){
    send.classList.toggle('busy', !!on);
    var other = !!(on && RUN_CONV_ID && RUN_CONV_ID !== CHAT_CONV_ID);
    send.setAttribute('aria-label', on ? (other ? '停止另一条会话的回答' : '停止生成') : '发送');
    send.setAttribute('title', on ? (other ? '停止另一条会话的回答' : '停止生成') : '发送');
  }
  /* 按钮是固定 40px 圆钮，忙碌态宽度不变，输入框留白也保持不变。 */
  if(ta) ta.style.paddingRight = '';
}
function stopRunningChat(){
  if(!CHAT_CTRL) return;
  try{ CHAT_CTRL.abort(); }catch(_e){ /* 已经结束的请求 abort 也会抛，忽略 */ }
  toast('已停止');
}
/* 流式读取 SSE：每个 `data:` 块回调一次；`: ping` 注释直接忽略。 */
async function readAgentSSE(resp, onEvent){
  var reader = resp.body.getReader();
  var dec = new TextDecoder('utf-8');
  var buf = '';
  function handle(block){
    var lines = block.split('\n'), data = [];
    for(var i = 0; i < lines.length; i++){
      var ln = lines[i].trim();
      if(ln.indexOf('data:') === 0) data.push(ln.slice(5).trim());
    }
    if(!data.length) return;
    var payload = data.join('\n');
    if(payload === '[DONE]') return;
    try{ onEvent(JSON.parse(payload)); }catch(e){ /* 半个 JSON 不打断整条流 */ }
  }
  while(true){
    var r = await reader.read();
    if(r.done) break;
    buf += dec.decode(r.value, {stream:true});
    if(buf.indexOf('\r') >= 0) buf = buf.replace(/\r\n/g, '\n');
    var idx;
    while((idx = buf.indexOf('\n\n')) >= 0){
      handle(buf.slice(0, idx));
      buf = buf.slice(idx + 2);
    }
  }
  if(buf.trim()) handle(buf);
}
function chatAutoScroll(){
  var log = document.getElementById('chatLog');
  if(!log) return;
  /* 只有用户本来就在底部时才跟随；向上翻看历史时不要把他拽回来 */
  if(log.scrollHeight - log.scrollTop - log.clientHeight < 180){
    log.scrollTop = log.scrollHeight;
  }
}
/* 一轮回答的最终渲染：思考默认折叠、答案 Markdown、工具链折叠、卡片。 */
function renderAgentResult(holder, r, q){
  if(r.answer || (r.trace || []).length) renderTrace(r, q);
  else renderTrace(null);
  var answer = r.answer || (r.detail || '（无回复）');
  holder.innerHTML = thinkingHtml(r.thinking, false,
    r.latency_ms ? r.latency_ms / 1000 : 0) + mdLite(answer);
  if((r.citations || []).length){
    var srcs = r.citations.map(function(c){
      return esc(c.label || c.tool) + (c.count > 1 ? ' ×' + c.count : '');
    }).join(' · ');
    holder.insertAdjacentHTML('beforeend',
      '<div class="src-line">数据来源：' + srcs + '</div>');
  }
  if(r.trace && r.trace.length){
    /* 工具轨迹**默认折叠** —— HR 不关心函数名和 JSON 参数。
       但有 [已拦截] 时**默认展开**：那是"这件事没做"的原因，不能藏起来。 */
    var blocked = r.trace.some(function(t){ return t.disabled; });
    var lines = groupTrace(r.trace).map(function(g){
      var meta = g.count > 1
        ? traceGroupMeta(g)
        : (Object.keys((g.items[0] || {}).args || {}).length
            ? JSON.stringify(g.items[0].args) : '');
      var at = meta ? '（' + esc(meta) + '）' : '';
      return '→ ' + esc(g.tool_label || g.tool) +
        (g.count > 1 ? ' ×' + g.count : '') + at +
        (g.write ? ' <b>[写·需确认]</b>' : '') +
        (g.disabled ? ' <b>[已拦截]</b>' : '') +
        (g.recovered ? ' <b>[已补做]</b>' : '');
    }).join('<br>');
    holder.insertAdjacentHTML('beforeend',
      '<details class="tracebox"' + (blocked ? ' open' : '') + '>' +
      '<summary>工具调用（' + r.trace.length + ' 次' + (blocked ? ' · 有被拦截的' : '') + '）</summary>' +
      '<div class="trace">' + lines + '</div></details>');
  }
  if(r.mode){
    var route = (r.route || []).filter(function(x){ return x && x !== 'core'; });
    var modeLabel = r.mode === 'simple' ? '快捷回复'
      : (r.mode === 'stopped' ? '已停止' : r.mode);
    var mt = r.metrics || {};
    var metricBits = [];
    if(mt.prompt_tokens) metricBits.push('输入 ' + fmtTokens(mt.prompt_tokens));
    if(mt.tool_ratio !== undefined) metricBits.push('工具占比 ' + Math.round((mt.tool_ratio || 0) * 100) + '%');
    if(mt.tool_calls) metricBits.push('工具错误 ' + (mt.tool_errors || 0) + '/' + mt.tool_calls);
    if(mt.cache_rate !== null && mt.cache_rate !== undefined) metricBits.push('缓存 ' + Math.round(mt.cache_rate * 100) + '%');
    holder.insertAdjacentHTML('beforeend',
      '<div class="trace">模式：' + esc(modeLabel) + ' · 轮数 ' + (r.rounds || 0) +
      ' · ' + (r.latency_ms || 0) + ' ms' +
      (metricBits.length ? ' · ' + esc(metricBits.join(' · ')) : '') +
      (route.length ? ' · 工具组：' + esc(route.join('/')) : '') +
      (r.skill_name ? ' · SOP：' + esc(r.skill_name) : '') + '</div>');
  }
  if(q) holder.setAttribute('data-q', q);
  if(r.run_id) holder.setAttribute('data-run-id', r.run_id);
  CHAT.push({role:'user', content:q}, {role:'assistant', content: r.answer || ''});
  if((r.drafts || []).length) renderDraftCards(holder, r.drafts);
  if((r.pending_proposals || []).length) renderProposalCards(holder, r.pending_proposals);
  if(r.session_id) CHAT_CONV_ID = r.session_id;
  finishLiveMessage(holder, q, r.run_id);
  loadConversations();          /* 首轮回答后，左侧出现这条会话的标题 */
  loadChatMetrics();            /* 运行指标随每次回答刷新 */
  if(r.context && r.context.window) renderContext(r.context);  /* 上下文用量 */
  renderOutputs(r);             /* 本次下载/草稿/提案 */
  killAutofill();
}
/* 给一条消息补上「编辑重发 / 重新生成」入口。 */
function addMsgActions(node, role){
  if(!node || node.querySelector('.msg-actions')) return;
  var user = role === 'user';
  node.insertAdjacentHTML('beforeend',
    '<div class="msg-actions"><button type="button" class="mdt-btn" ' +
    (user ? 'data-edit-msg' : 'data-retry') + '>' +
    (user ? '编辑重发' : '重新生成') + '</button></div>');
}
/* 一轮回答结束后：回答和它前面那条提问都挂上对应入口与 run_id。 */
function finishLiveMessage(holder, q, runId){
  if(!holder) return;
  if(runId) holder.setAttribute('data-run-id', runId);
  var prev = holder.previousElementSibling;
  while(prev && !(prev.classList && prev.classList.contains('msg'))){
    prev = prev.previousElementSibling;
  }
  if(prev && prev.classList.contains('user')){
    if(runId) prev.setAttribute('data-run-id', runId);
    addMsgActions(prev, 'user');
  }
  addMsgActions(holder, 'assistant');
}
/* 编辑/重发前，先在服务端把这条提问之后的旧分支软隐藏（审计记录保留）。 */
async function truncateConversationAt(runId){
  if(!CHAT_CONV_ID || !runId) return {ok:true};
  return await api('/api/agent/conversations/' + encodeURIComponent(CHAT_CONV_ID) + '/truncate', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({before_run_id: Number(runId)})
  });
}
/* 从某条用户消息开始重新发送：去掉它之后的旧回答与后续轮次。 */
async function resendFrom(userNode, text){
  if(!userNode || !text) return;
  if(CHAT_CTRL){ toast('回答还在生成中，稍后再试', 'info'); return; }
  var log = document.getElementById('chatLog');
  var all = Array.prototype.slice.call(log.querySelectorAll('.msg'));
  var idx = all.indexOf(userNode);
  if(idx < 0) return;
  var runId = Number(userNode.getAttribute('data-run-id') || 0);
  if(runId > 0){
    var tr = await truncateConversationAt(runId);
    if(tr && tr.__err){ toast('重发失败：' + tr.__err, 'danger'); return; }
  }
  all.slice(idx).forEach(function(n){ n.remove(); });
  CHAT = CHAT.slice(0, Math.min(idx, CHAT.length));
  CHAT_OUTPUTS = [];
  renderTrace(null);
  renderOutputs(null, true);
  renderContext(null);
  syncChatSidebar();
  var inp = document.getElementById('chatIn');
  if(inp){ inp.value = text; chatGrow(inp); inp.focus(); }
  askAgent();
}
function retryAgent(btn){
  if(CHAT_CTRL){ toast('回答还在生成中，稍后再试', 'info'); return; }
  var holder = btn.closest('.msg.assistant');
  if(!holder) return;
  var prev = holder.previousElementSibling;
  while(prev && !(prev.classList && prev.classList.contains('msg'))){
    prev = prev.previousElementSibling;
  }
  if(!prev || !prev.classList.contains('user')) return;
  var q = (prev.getAttribute('data-q') || prev.innerText || '').trim();
  if(q) resendFrom(prev, q);
}
function editUserMessage(btn){
  var node = btn.closest('.msg.user');
  if(!node || node.querySelector('.msg-edit')) return;
  if(CHAT_CTRL){ toast('回答还在生成中，稍后再编辑', 'info'); return; }
  var text = node.getAttribute('data-q') || node.innerText || '';
  var original = node.innerHTML;
  node.innerHTML =
    '<textarea class="msg-edit" rows="2" aria-label="编辑这条提问"></textarea>' +
    '<div class="msg-actions msg-edit-actions">' +
      '<button type="button" class="mdt-btn" data-edit-cancel>取消</button>' +
      '<button type="button" class="btn primary sm" data-edit-save>重发</button>' +
    '</div>';
  var ta = node.querySelector('.msg-edit');
  ta.value = text;
  ta.focus();
  try{ ta.setSelectionRange(text.length, text.length); }catch(e){}
  function cancel(){ node.innerHTML = original; }
  function save(){
    var v = (ta.value || '').trim();
    if(!v){ ta.focus(); return; }
    if(CHAT_CTRL){ toast('回答还在生成中，稍后再试', 'info'); return; }
    resendFrom(node, v);
  }
  ta.addEventListener('keydown', function(ev){
    if(ev.key === 'Enter' && !ev.shiftKey){ ev.preventDefault(); save(); }
    if(ev.key === 'Escape'){ ev.preventDefault(); cancel(); }
  });
  node.querySelector('[data-edit-cancel]').addEventListener('click', cancel);
  node.querySelector('[data-edit-save]').addEventListener('click', save);
}
async function askAgentNonStream(q, holder, gen){
  if(gen == null) gen = ++CHAT_GEN;
  if(gen !== CHAT_GEN) return;
  var convId = CHAT_CONV_ID;
  holder.innerHTML = '<span class="hint">思考中…</span>';
  var ctrl = new AbortController();
  CHAT_CTRL = ctrl;
  chatBusy(true);
  renderTraceRunning(q);
  startTracePoll(q, holder);
  var r = await api('/api/agent/chat', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({message: q, history: CHAT, session_id: convId}),
    signal: ctrl.signal});
  if(CHAT_CTRL === ctrl){
    CHAT_CTRL = null;
    chatBusy(false);
    stopTracePoll();
  }
  var viewing = (convId === CHAT_CONV_ID);
  if(gen !== CHAT_GEN){
    /* 期间切走了又切回来：服务端已经入库，重新读一次历史即可。 */
    if(viewing) openConversation(convId);
    else loadConversations();
    return;
  }
  if(r.__err && /abort/i.test(r.__err)){
    holder.innerHTML = '<span class="hint">已停止。</span>';
    renderTrace(null);
    return;
  }
  if(r.__err && !r.answer){
    holder.innerHTML = '<span class="hint">' + esc(r.__err) + '</span>';
    renderTrace(null);
    return;
  }
  renderAgentResult(holder, r, q);
}
async function askAgent(){
  var inp = document.getElementById('chatIn');
  var q = (inp.value || '').trim();
  if(!q) return;
  if(CHAT_CTRL){
    toast(RUN_CONV_ID && RUN_CONV_ID !== CHAT_CONV_ID
      ? '另一条会话的回答仍在生成，切回去可停止或等待完成'
      : '回答还在生成，可点右侧停止或稍候', 'info');
    return;
  }
  if(!CHAT_CONV_ID) CHAT_CONV_ID = newConvId();
  var gen = ++CHAT_GEN;
  var convId = CHAT_CONV_ID;
  ensureConvInList(convId, q);
  inp.value = '';
  chatGrow(inp);
  pushMsg('user', q);
  var holder = pushMsg('assistant', '');
  holder.innerHTML = '<div class="stream-status hint">正在思考…</div>' +
    '<div class="stream-answer"></div>';
  /* 这一轮的状态放在全局：切到别的会话时 DOM 换掉，但内容继续累积；
     切回来时 openConversation 会把 RUN_HOLDER 重新绑到新的气泡上。 */
  var run = {liveThink:'', liveAns:'', statusBase:'正在思考…'};
  RUN_CONV_ID = convId; RUN_Q = q; RUN_HOLDER = holder; RUN_STATE = run;
  var answerTimer = null;
  var sawAny = false;
  var t0 = Date.now(), lastEvent = Date.now(), tick = null, timedOut = false;
  function viewing(){ return RUN_CONV_ID === CHAT_CONV_ID && !!RUN_HOLDER; }
  function setStatus(t){
    if(t) run.statusBase = t;
    if(!viewing()) return;
    var s = RUN_HOLDER.querySelector('.stream-status');
    if(!s) return;
    var sec = Math.round((Date.now() - t0) / 1000);
    var extra = run.liveAns ? (' · 已 ' + run.liveAns.length + ' 字') : '';
    s.textContent = run.statusBase + ' · ' + sec + 's' + extra;
  }
  function ensureThink(){
    if(!viewing()) return null;
    var h = RUN_HOLDER;
    var body = h.querySelector('.think-body');
    if(body) return body;
    var d = document.createElement('div');
    d.className = 'think-live';
    d.innerHTML = '<div class="think-live-h">正在思考</div><div class="think-body"></div>';
    h.insertBefore(d, h.querySelector('.stream-answer'));
    return d.querySelector('.think-body');
  }
  function flushAnswer(){
    if(answerTimer){ clearTimeout(answerTimer); answerTimer = null; }
    if(!viewing()) return;
    var a = RUN_HOLDER.querySelector('.stream-answer');
    if(a) a.innerHTML = run.liveAns ? mdLite(run.liveAns) : '';
    chatAutoScroll();
  }
  function scheduleAnswer(){
    if(answerTimer) return;
    answerTimer = setTimeout(function(){ answerTimer = null; flushAnswer(); }, 90);
  }
  function onEvent(ev){
    if(!ev || !ev.type) return;
    /* 内容始终累积，即使用户已经切到别的会话 */
    sawAny = true;
    lastEvent = Date.now();
    if(ev.type === 'thinking_delta'){
      run.liveThink += ev.text || '';
      if(viewing()){
        var b = ensureThink();
        if(b){ b.textContent = run.liveThink; b.scrollTop = b.scrollHeight; }
        setStatus('正在思考…');
        chatAutoScroll();
      } else {
        run.statusBase = '正在思考…';
      }
    } else if(ev.type === 'answer_delta'){
      run.liveAns += ev.text || '';
      if(viewing()){ setStatus('正在生成回答…'); scheduleAnswer(); }
      else run.statusBase = '正在生成回答…';
    } else if(ev.type === 'answer_reset'){
      run.liveAns = '';
      flushAnswer();
    } else if(ev.type === 'tool_start'){
      setStatus('正在调用：' + (ev.tool_label || ev.tool || '工具') + '…');
    } else if(ev.type === 'tool_result'){
      setStatus((ev.ok === false ? '工具返回错误：' : '已完成：') +
        (ev.tool_label || ev.tool || '工具'));
    } else if(ev.type === 'answer_complete'){
      setStatus('回答已生成，正在收尾…');
    } else if(ev.type === 'error'){
      setStatus('出错：' + (ev.message || '未知错误'));
    }
  }
  CHAT_CTRL = new AbortController();
  CHAT_STREAMING = true;
  chatBusy(true);
  renderTraceRunning(q);
  startTracePoll(q, holder);
  renderConvList();
  var done = null;
  tick = setInterval(function(){
    if(RUN_STATE !== run){
      if(tick){ clearInterval(tick); tick = null; }
      return;
    }
    var sec = Math.round((Date.now() - t0) / 1000);
    if(!sawAny && sec >= 15){
      run.statusBase = '模型响应较慢，仍在等待…（可点停止）';
    }
    /* 2 分钟没有任何增量就认为链路卡住，主动停止，避免界面永远停在“正在生成” */
    if(Date.now() - lastEvent > 120000){
      timedOut = true;
      run.statusBase = '模型长时间无响应，已停止';
      setStatus();
      if(CHAT_CTRL) CHAT_CTRL.abort();
      return;
    }
    setStatus();
  }, 1000);
  function finish(){
    var viewingNow = (RUN_CONV_ID === CHAT_CONV_ID && !!RUN_HOLDER);
    var liveHolder = RUN_HOLDER;
    CHAT_CTRL = null;
    CHAT_STREAMING = false;
    RUN_CONV_ID = null; RUN_Q = ''; RUN_HOLDER = null; RUN_STATE = null;
    chatBusy(false);
    stopTracePoll();
    if(tick){ clearInterval(tick); tick = null; }
    if(answerTimer){ clearTimeout(answerTimer); answerTimer = null; }
    renderConvList();
    return viewingNow ? liveHolder : null;
  }
  try{
    var resp = await fetch('/api/agent/chat/stream', {method:'POST',
      headers: hdr(),
      body: JSON.stringify({message: q, history: CHAT, session_id: convId}),
      signal: CHAT_CTRL.signal});
    if(resp.status === 401 && AUTH_ENABLED){
      setToken(''); ME = null;
      if(!LOGIN_SHOWN) showLogin('登录已失效，请重新登录');
    }
    if(!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
    await readAgentSSE(resp, function(ev){
      if(ev && ev.type === 'done') done = ev;
      else onEvent(ev);
    });
    if(!done) throw new Error('流式连接结束但没有收到最终答复');
    var live = finish();
    if(live){
      renderAgentResult(live, done, q);
    } else {
      /* 用户已经切走：答案由服务端入库，刷新会话列表即可，
         不要碰当前会话的 DOM / CHAT。 */
      loadConversations();
    }
  }catch(e){
    var aborted = String((e && e.name) || '') === 'AbortError' || /abort/i.test(String(e));
    var live = finish();
    loadConversations();       /* 中止/失败时把本地临时会话与服务端对齐 */
    if(!live){
      /* 切走后才结束/被停止：不污染当前会话，也不把半成品塞进当前 CHAT。 */
      loadConversations();
      return;
    }
    if(aborted){
      if(run.liveAns || run.liveThink){
        /* 已经生成的内容不能因为点停止就丢掉——尤其答案看起来已经出完、
           只是服务端还在收尾时，保留它比显示一句"已停止"有用得多。 */
        live.innerHTML = thinkingHtml(run.liveThink, false,
          Math.round((Date.now() - t0) / 1000)) + mdLite(run.liveAns) +
          '<div class="trace">' +
          (timedOut ? '模型长时间无响应，已保留已生成内容。' : '已停止，已保留已生成内容。') +
          '</div>';
        CHAT.push({role:'user', content:q}, {role:'assistant', content:run.liveAns});
      } else {
        live.innerHTML = '<span class="hint">' +
          (timedOut ? '模型长时间无响应，已停止。' : '已停止。') + '</span>';
      }
      finishLiveMessage(live, q, null);
      renderTrace(null);
      return;
    }
    /* 流式不可用（老代理/老端点）→ 自动退回非流式；已经显示了一半则保留半成品。 */
    if(run.liveAns || run.liveThink){
      live.innerHTML = thinkingHtml(run.liveThink, false,
        Math.round((Date.now() - t0) / 1000)) + mdLite(run.liveAns) +
        '<div class="trace">流式连接中断：' + esc(String((e && e.message) || e)) + '</div>';
      CHAT.push({role:'user', content:q}, {role:'assistant', content:run.liveAns});
      finishLiveMessage(live, q, null);
      return;
    }
    if(sawAny){
      /* 已经执行过工具（可能已生成提案），绝不能重跑一遍造成重复。
         如实告诉 HR 连接断了，让他刷新后看提案列表。 */
      live.innerHTML = '<div class="trace">流式连接中断：' +
        esc(String((e && e.message) || e)) + '</div>';
      finishLiveMessage(live, q, null);
      renderTrace(null);
      return;
    }
    await askAgentNonStream(q, live, gen);
  }
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
      openDraftInMailer(raw[d.id] || d);
    });
  });
}
/* 提案一多，同一类操作会刷满一屏（例如批量归岗 20 条）。
   按工具类型分组：1–3 条直接展开；超过 3 条默认折叠，标题写明类型与条数，
   需要逐条核对时再展开。批量按钮仍在底部，不展开也能一次处理。 */
function groupProposalRows(props, rowFn){
  var groups = [], byKey = {};
  (props || []).forEach(function(p){
    var key = String(p.tool || p.tool_label || 'other');
    if(!byKey[key]){
      byKey[key] = {label: p.tool_label || p.tool || '其他提案', items: []};
      groups.push(byKey[key]);
    }
    byKey[key].items.push(p);
  });
  return groups.map(function(g){
    var rows = g.items.map(rowFn).join('');
    if(g.items.length <= 3) return rows;
    return '<details class="propgroup"><summary>' + esc(g.label) +
      ' · ' + g.items.length + ' 条<span class="propgroup-hint">展开核对</span></summary>' +
      rows + '</details>';
  }).join('');
}
function renderProposalCards(holder, props){
  var el = document.createElement('div');
  el.className = 'dcard prop';
  /* 每条提案**直接在卡片上确认/拒绝**（业务方 2026-10-21：原版有、v2 缺）。
     原来这里只有一句"需要你在对应页面确认" —— 等于把 HR 从助手页赶去人才库，
     而助手恰恰是他刚提问的地方。写操作仍需人点头，只是把这个"点头"放到手边。 */
  var pendingCount = props.filter(function(p){
    return (p.status || '待确认') === '待确认';
  }).length;
  var batchCount = props.filter(function(p){
    return (p.status || '待确认') === '待确认' && p.current_turn;
  }).length;
  el.innerHTML = '<div class="dcard-t">待确认提案（' + props.length + '）</div>' +
    groupProposalRows(props, function(p){
      /* ⚠️ 助手返回的字段是 `proposal_id`，不是 `id`。
         原来写 `p.id` → data-prop="undefined" → 请求 /api/proposals/NaN/decide
         → FastAPI 422（detail 是对象数组）→ 界面只弹 `[object Object]`。 */
      var pid = (p.proposal_id != null) ? p.proposal_id : p.id;
      var st = p.status || '待确认';
      var pending = st === '待确认';
      var ch = (p.changes || []).filter(function(c){ return c && c.label; })
        .map(function(c){
          return '<div class="proprow-c">' + esc(c.label) + '：' +
            esc(c.before || '（空）') + ' <b>→</b> ' + esc(c.after || '（空）') + '</div>';
        }).join('');
      return '<div class="proprow" data-pid="' + esc(pid) + '"' +
        (p.current_turn ? ' data-current="1"' : '') + '>' +
        '<div class="proprow-t">' + esc(p.title || p.summary || p.description || '') +
          (p.risk ? '<span class="prisk r' + esc(p.risk) + '">' + esc(p.risk) + '风险</span>' : '') +
        '</div>' + ch +
        (pending
          ? '<div class="proprow-a">' +
              '<button class="btn primary sm" data-prop="' + esc(pid) + '" data-d="approve">同意执行</button>' +
              '<button class="btn sm" data-prop="' + esc(pid) + '" data-d="reject">拒绝</button>' +
            '</div>'
          : '<div class="proprow-a"><span class="proprow-st">' + esc(st) + '</span></div>') +
        '</div>';
    }) +
    (batchCount > 1
      ? '<div class="dcard-a propbatch">' +
          '<button class="btn primary sm" data-prop-all="approve">' +
            '全部同意（' + batchCount + '）</button>' +
          '<button class="btn sm" data-prop-all="reject">全部拒绝</button>' +
        '</div>'
      : '') +
    '<div class="dcard-a"><span class="hint">同意后立即执行并写审计；拒绝也留痕。' +
      '写操作不会自动生效。</span></div>';
  holder.appendChild(el);
  Array.prototype.forEach.call(el.querySelectorAll('[data-prop]'), function(b){
    b.addEventListener('click', function(){
      var pid = Number(b.dataset.prop);
      if(!pid || isNaN(pid)){ toast('这条提案缺少编号，请刷新后重试'); return; }
      /* decideProp 会先把同一提案的按钮一起标灰并显示“已选择”，
         用户在确认框点取消时再恢复。 */
      decideProp(pid, b.dataset.d, b);
    });
  });
  Array.prototype.forEach.call(el.querySelectorAll('[data-prop-all]'), function(b){
    b.addEventListener('click', function(){ decideAllCardProps(el, b.dataset.propAll); });
  });
}
/* 一句话批量任务：动作已由助手拆好，HR 只需要在这里点一次“全部同意”。 */
async function decideAllCardProps(el, decision){
  var pids = [];
  Array.prototype.forEach.call(el.querySelectorAll('.proprow[data-pid]'), function(row){
    if(!row.querySelector('[data-prop]')) return;   /* 已处理的行不参与批量 */
    if(row.getAttribute('data-current') !== '1') return;  /* 旧提案不批量误执行 */
    var pid = Number(row.getAttribute('data-pid'));
    if(pid && !isNaN(pid)) pids.push(pid);
  });
  if(!pids.length) return;
  var approve = decision === 'approve';
  var ok = await askConfirm({
    title: approve ? '全部同意并执行' : '全部拒绝',
    danger: !approve,
    okText: approve ? ('同意并执行 ' + pids.length + ' 条') : '全部拒绝',
    body: (approve ? '将执行这 ' : '将拒绝这 ') + pids.length +
      ' 条提案，全部写入审计。' + (approve ? '执行后不能自动撤销。' : '')
  });
  if(!ok) return;
  var success = 0, failed = 0;
  for(var i = 0; i < pids.length; i++){
    var r = await api('/api/proposals/' + pids[i] + '/decide', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({decision: approve ? 'approve' : 'reject'})
    });
    if(r && !r.__err){
      success++;
      settlePropCard(pids[i], approve ? 'approve' : 'reject');
    } else failed++;
  }
  toast((approve ? '已执行 ' : '已拒绝 ') + success + ' 条' +
        (failed ? ('，' + failed + ' 条失败') : ''), failed ? 'warn' : 'ok');
  if(CHAT_CONV_ID) openConversation(CHAT_CONV_ID);
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
      '<b role="button" tabindex="0" aria-label="移除这个面试单位" onkeydown="keyClick(event)" ' +
      'onclick="multiDelIvUnit(' + i + ')" title="移除">×</b></span>';
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
var PROPS = [], PROPS_LOADED = false;
/* 提案摘要里的姓名 → 可点开档案的链接。
   后端 `/api/proposals` 已把每条提案的 candidate_id / candidate_name 解析好
   （set_stage 提案存的是 application_id，由后端换成候选人），这里只替换
   摘要里**第一次出现**的姓名 —— 现有摘要都以姓名开头。
   找不到姓名（例如"给投递 #12 写备注"）就保持纯文本，不硬造链接。 */
function propSummaryHtml(p){
  var raw = String(p.summary || '');
  var name = String(p.candidate_name || '');
  var cid = Number(p.candidate_id || 0);
  if(!cid || !name) return esc(raw);
  var at = raw.indexOf(name);
  if(at < 0) return esc(raw);
  return esc(raw.slice(0, at)) +
    '<a class="plink" href="javascript:void(0)" ' +
    'onclick="openPropCandidate(' + cid + ',event)" title="打开档案">' +
    esc(name) + '</a>' + esc(raw.slice(at + name.length));
}
/* 点姓名：先收起提案浮层，再在右侧打开档案（pick 是列表行同款入口） */
function openPropCandidate(cid, ev){
  if(ev){ ev.preventDefault(); ev.stopPropagation(); }
  var box = document.getElementById('propBox');
  if(box) box.classList.remove('on');
  var btn = document.getElementById('btnProp');
  if(btn) btn.classList.remove('on');
  pick(Number(cid));
}
function toggleProps(){
  var box = document.getElementById('propBox');
  var open = box.classList.toggle('on');
  document.getElementById('btnProp').classList.toggle('on', open);
  if(open) loadProps();
}
function propRowHtml(p){
  return '<div class="prow" id="prop-' + p.id + '">' +
    '<div class="ptext">' + propSummaryHtml(p) + '</div>' +
    '<span class="prisk r' + esc(p.risk || '低') + '">' + esc(p.risk || '低') + '风险</span>' +
    '<span class="pacts">' +
      '<button class="btn primary sm" data-d="approve" onclick="decideProp(' + p.id + ',\'approve\',this)">同意</button>' +
      '<button class="btn sm" data-d="reject" onclick="decideProp(' + p.id + ',\'reject\',this)">拒绝</button>' +
    '</span></div>';
}
/* 只更新计数，不重建整个提案列表（v4.0.15：原来每处理一条都整块重绘） */
function updatePropCount(){
  /* 没打开过提案面板时 PROPS 是空数组，不能据此把按钮改成「（0）」——
     那会让 HR 以为没有待办。只有真正加载过才用本地计数。 */
  if(!PROPS_LOADED) return;
  var btn = document.getElementById('btnProp');
  if(btn) btn.textContent = '待确认提案（' + PROPS.length + '）';
  var n = document.getElementById('propCount');
  if(n) n.textContent = '共 ' + PROPS.length + ' 条待确认';
  if(!PROPS.length){
    var box = document.getElementById('propBox');
    if(box && box.classList.contains('on')){
      box.innerHTML = '<div class="hint" style="padding:10px">没有待确认的提案</div>';
    }
  }
}
async function loadProps(){
  var box = document.getElementById('propBox');
  box.innerHTML = '<div class="hint" style="padding:8px">读取提案…</div>';
  var r = await api('/api/proposals');
  if(r.__err){
    /* 失败时**不能**把 PROPS 当空数组并置 PROPS_LOADED：那会把按钮改成
       「待确认提案（0）」，真实待办被藏起来。保持未加载态，只显示错误。 */
    box.innerHTML = '<div class="hint" style="padding:8px">提案读取失败：' + esc(r.__err) +
      '<br><button class="btn sm" style="margin-top:6px" onclick="loadProps()">重试</button></div>';
    return;
  }
  PROPS = (r.items || []).filter(function(x){ return x.status === '待确认'; });
  PROPS_LOADED = true;
  if(!PROPS.length){
    updatePropCount();
    box.innerHTML = '<div class="hint" style="padding:10px">没有待确认的提案</div>';
    return;
  }
  box.innerHTML = '<div class="prow" style="border-bottom:1px solid var(--line)">' +
    '<span class="ptext hint" id="propCount">共 ' + PROPS.length + ' 条待确认</span>' +
    '<span class="pacts">' +
      '<button class="btn sm" onclick="decideAllProps(\'approve\')">全部确认执行</button>' +
      '<button class="btn sm" onclick="decideAllProps(\'reject\')">全部拒绝</button>' +
    '</span></div>' + groupProposalRows(PROPS, propRowHtml);
  updatePropCount();
}
/* 批量决定：逐条走同一个 decide 接口（每条都写审计），失败不中断，最后汇总 */
async function decideAllProps(decision){
  /* 面板可能是几分钟前打开的；批量执行前重新取一次，避免只处理旧快照里的第一条/前几条。 */
  var fresh = await api('/api/proposals');
  if(fresh.__err){ toast('读取提案失败：' + fresh.__err, 'danger'); return; }
  var ids = (fresh.items || []).filter(function(x){ return x.status === '待确认'; })
    .map(function(p){ return p.id; });
  if(!ids.length){ toast('没有待确认的提案'); return; }
  var ok = await askConfirm({title: decision === 'approve' ? '全部确认执行' : '全部拒绝',
    danger: decision === 'reject',
    body: '将对 ' + ids.length + ' 条提案' +
          (decision === 'approve' ? '逐条执行（会真的改档案，写审计）' : '逐条拒绝（也写审计）') + '。继续？'});
  if(!ok) return;
  var done = 0, fail = 0;
  for(var i = 0; i < ids.length; i++){
    toast('正在处理 ' + (i + 1) + ' / ' + ids.length + ' 条…');
    var r = await api('/api/proposals/' + ids[i] + '/decide', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({decision: decision})});
    if(r.__err) fail++;
    else { done++; settlePropCard(ids[i], decision); }
  }
  toast('已处理 ' + done + ' 条' + (fail ? ('，失败 ' + fail + ' 条') : ''));
  /* 批量是一次性操作，重建一次列表可以接受；单条决定见 decideProp 的局部更新 */
  loadProps();
  load({push:false});
}
/* 同一提案可能同时出现在「对话卡片」和右侧「待确认提案」面板里，
   两处必须一起变灰、一起显示用户点了哪个；只停被点的那个按钮等于没停。 */
function propActionAreas(pid){
  return document.querySelectorAll(
    '[data-pid="' + pid + '"] .proprow-a, #prop-' + pid + ' .pacts');
}
function markPropPending(pid, decision){
  var choice = decision === 'approve' ? '同意执行' : '拒绝';
  Array.prototype.forEach.call(propActionAreas(pid), function(area){
    /* 只改按钮状态，不替换 innerHTML：对话卡片上的按钮是 renderProposalCards
       一次性绑的监听器，innerHTML 一换，取消确认后就再也点不动了。 */
    Array.prototype.forEach.call(area.querySelectorAll('button'), function(b){
      b.disabled = true;
      b.classList.toggle('chosen', b.getAttribute('data-d') === decision);
    });
    var old = area.querySelector('.propchoice');
    if(old) old.remove();
    var tag = document.createElement('span');
    tag.className = 'propchoice ' + decision;
    tag.textContent = '已选择：' + choice;
    area.appendChild(tag);
  });
}
function restorePropPending(pid){
  Array.prototype.forEach.call(propActionAreas(pid), function(area){
    var old = area.querySelector('.propchoice');
    if(old) old.remove();
    Array.prototype.forEach.call(area.querySelectorAll('button'), function(b){
      b.disabled = false;
      b.classList.remove('chosen');
    });
  });
}
function settlePropCard(pid, decision){
  var label = decision === 'approve' ? '已同意执行' : '已拒绝';
  var cls = decision === 'approve' ? 'approve' : 'reject';
  Array.prototype.forEach.call(propActionAreas(pid), function(area){
    area.innerHTML = '<span class="proprow-st ' + cls + '">' + label + '</span>';
  });
  Array.prototype.forEach.call(document.querySelectorAll('[data-prop="' + pid + '"]'),
    function(b){ b.disabled = true; });
  Array.prototype.forEach.call(document.querySelectorAll('.proprow[data-pid="' + pid + '"]'),
    function(row){ row.classList.add('done'); });
  var row = document.getElementById('prop-' + pid);
  if(row) row.classList.add('done');
}
async function decideProp(pid, decision, btn){
  var q = decision === 'approve'
    ? '确认执行这条提案吗？\n\n它会真的改档案（写审计）。'
    : '拒绝这条提案？\n\n拒绝也会写入审计。';
  /* 用户一点就先标出“选了哪一个”，并把同一提案的另一处按钮一起锁住；
     取消确认时再恢复，避免点错后卡死。 */
  markPropPending(pid, decision);
  var ok = await askConfirm({title: decision === 'approve' ? '同意提案' : '拒绝提案',
    body: q, danger: decision === 'reject',
    okText: decision === 'approve' ? '同意并执行' : '拒绝'});
  if(!ok){ restorePropPending(pid); return; }
  var r;
  try{
    r = await api('/api/proposals/' + pid + '/decide', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({decision: decision})});
  }catch(e){
    restorePropPending(pid);
    toast('执行失败：' + String((e && e.message) || e), 'danger');
    return;
  }
  if(r.__err){ toast(r.__err); restorePropPending(pid); return; }
  /* 局部更新：保留这一行并标成"已处理"，只把计数减一；不重建整块提案列表。
     这样 40+ 条时不会每次点同意都闪一下，也能看见这条已经处理过了。 */
  settlePropCard(pid, decision);
  PROPS = PROPS.filter(function(x){ return Number(x.id) !== Number(pid); });
  updatePropCount();
  toast(r.message || (decision === 'approve' ? '已执行' : '已拒绝'));
  load({push:false});
}
/* ---------- 批量重新分析 / 分析未分析的人 ---------- */
async function batchReanalyze(){
  var ids = Object.keys(SEL).filter(function(k){ return SEL[k]; }).map(Number);
  var scope = ids.length ? ('勾选的 ' + ids.length + ' 人') : '全部候选人';
  if(!ids.length){
    /* 未勾选 = 全部。先把 id 取出来分块提交：后端一次只跑 10 人，
       且“全部重跑”不能用 remaining 循环（同一批人会反复被选中）。 */
    var all = await api('/api/candidates?page_size=100000');
    if(all.__err){ toast(all.__err, 'danger'); return; }
    ids = (all.items || []).map(function(x){ return Number(x.id); }).filter(Boolean);
  }
  if(!ids.length){ toast('没有可分析的候选人', 'warn'); return; }
  var ok = await askConfirm({title: '批量重新分析',
    body: '对' + scope + '做完整重新分析？\n\n' +
          '会重新解析原件、用模型重抽字段、重算建议档位与分析结论；\n' +
          '**不会覆盖** HR 已确认的档位、阶段、岗位，以及人工更正过的字段。\n' +
          '每人约 6–15 秒，人数多时需要较长时间。',
    okText: '开始分析'});
  if(!ok) return;
  var total = 0, failed = 0, done = 0;
  toast('开始重新分析…');
  for(var i = 0; i < ids.length; i += 10){
    var chunk = ids.slice(i, i + 10);
    var r = await api('/api/candidates/reanalyze-batch', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({limit: 10, only_heuristic: false, ids: chunk})});
    if(r.__err){ toast('分析中断：' + r.__err, 'danger'); break; }
    total += Number(r.analyzed) || 0;
    failed += Number(r.failed) || 0;
    done += chunk.length;
    toast('已处理 ' + done + ' / ' + ids.length + ' 人…');
    /* 整块都没成功 = 模型/解析有问题，继续只会空转 */
    if(!(Number(r.analyzed) > 0) && Number(r.failed) > 0){
      toast('这一批没有成功（失败 ' + r.failed + ' 人），已停下；请检查模型后重试', 'warn');
      break;
    }
  }
  toast('批量重新分析完成：成功 ' + total + ' 人'
        + (failed ? '，失败 ' + failed + ' 人' : ''), failed ? 'warn' : 'ok');
  load();
}
async function analyzePending(){
  /* 只处理“从未用大模型分析过”的人：extract_mode 不是 llm/llm+rule。
     每轮 10 人；重跑成功后 extract_mode 会变成 llm，自动从下一轮排除。 */
  var total = 0, failed = 0, rounds = 0;
  toast('正在分析未分析的人…');
  while(rounds < 300){
    var r = await api('/api/candidates/reanalyze-batch', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({limit: 10, only_heuristic: true})});
    if(r.__err){ toast(r.__err, 'danger'); break; }
    total += Number(r.analyzed) || 0;
    failed += Number(r.failed) || 0;
    rounds++;
    if(!(Number(r.analyzed) > 0)) break;
    toast('已分析 ' + total + ' 人，还剩 ' + (r.remaining || 0) + ' 人…');
    if(!r.remaining) break;
  }
  if(total) toast('已分析 ' + total + ' 人' + (failed ? '，失败 ' + failed + ' 人' : ''), 'ok');
  else toast('没有未分析的人', 'warn');
  load();
}

/* ---------- 结论标签：取值范围**按阶段限定**（stages.json 的 outcomes）----------
   待面试/已面试 → 拟录用 · 不录用；已发放offer → 已签约 · 拒签；其它阶段不允许打结论。
   之前 v2 固定给「未判/拟录用/不录用」，在「已发放offer」阶段提交必然被服务端 422 ——
   业务方看到的就是"下拉能选、点了没反应"。 */
function outcomeField(d){
  /* v4.0.17：面试结论只保留 未判/拟录用/不录用。
     已签约/拒签/毁约已经挪到「签约情况」，不再和面试结论抢同一个字段。 */
  var cur = (d.outcome === '拟录用' || d.outcome === '不录用') ? d.outcome : '';
  var badge = outcomeChip(cur, d.stage);
  var opts = ['', '拟录用', '不录用'];
  var order = STAGE_CFG.order || [];
  var stageIdx = order.indexOf((d.stage || '').trim());
  var interviewIdx = order.indexOf('已面试');
  var waitIdx = order.indexOf('待面试');
  if(stageIdx < 0 || (waitIdx >= 0 && stageIdx < waitIdx)){
    return '<span class="hint">进入「待面试」后可填面试结论</span>';
  }
  var autoAdvance = stageIdx >= 0 && interviewIdx >= 0 && stageIdx < interviewIdx;
  var title = autoAdvance
    ? '面试结论（选择后会从当前阶段自动推进到「已面试」）'
    : '面试结论（只改标签，不改变当前阶段）';
  return '<span class="ivout">' + badge +
    '<select id="ivOutcome" class="vsel" title="' + esc(title) + '">' +
    opts.map(function(v){
      return '<option value="' + esc(v) + '"' + (cur === v ? ' selected' : '') + '>' +
        (v || '未判') + '</option>';
    }).join('') + '</select>' +
    '<button class="btn sm btn-danger" onclick="clearOutcome()">清空结论</button></span>';
}

/* 签约情况下拉：未签约 / 已签约 / 拒签 / 毁约。
   已签约时如果没有签约日期，自动补今天 —— 招聘进度按“已签约 + 签约日期”同步。 */
function signStatusField(d){
  var cur = d.sign_status || '';
  if(!cur){
    if(d.outcome === '已签约') cur = '已签约';
    else if(d.outcome === '拒签') cur = '拒签';
    else if(d.signed_at) cur = '已签约';
    else cur = '未签约';
  }
  var opts = ['未签约', '已签约', '拒签', '毁约'];
  return '<select id="obSignStatus" title="签约情况" onchange="signStatusChanged(this)">' +
    opts.map(function(v){
      return '<option value="' + esc(v) + '"' + (cur === v ? ' selected' : '') + '>' +
        esc(v) + '</option>';
    }).join('') + '</select>';
}
function onboardStatusField(d){
  var cur = String(d.onboard_status || '');
  var opts = ['未入职', '已入职', '放弃'];
  return '<select id="obOnboardStatus" title="人员状态" onchange="onboardStatusChanged(this)">' +
    opts.map(function(v){
      return '<option value="' + esc(v) + '"' + (cur === v ? ' selected' : '') + '>' +
        esc(v) + '</option>';
    }).join('') + '</select>';
}
function onboardStatusChanged(sel){
  if(sel.value === '已入职'){
    toast('保存后记录入职状态；入职日期默认今天', 'ok');
  }
}
function signStatusChanged(sel){
  if(sel.value !== '已签约') return;
  var dt = document.getElementById('obSigned');
  if(!dt || dt.value) return;
  var d = new Date();
  var s = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
          String(d.getDate()).padStart(2, '0');
  dt.value = s;
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
  /* v4.0.17：面试结论走 interview-info（值域固定为拟录用/不录用），
     不再走阶段接口 —— 阶段接口的 outcome 还承担“已签约/拒签”的旧动作，
     两者混用会把面试结论覆盖掉。 */
  var r = await api('/api/applications/' + aid + '/interview-info', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({outcome: v || ''})});
  if(r.__err){ toast(r.__err); return; }
  var msg = v ? ('已把面试结论改为「' + v + '」') : '已清除面试结论（未判）';
  if(r.advanced) msg += '，并自动推进到「已面试」';
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
/* 一键复制基本信息（业务方 2026-10-21，2026-10-30 收窄）。
   收窄原因：HR 实际用途是"把人发给面试官 / 发到群里"，只要**姓名 + 性别 + 两个联系方式**；
   学历、学校、专业、四六级都在简历里，复制过去反而是噪音。
   取值顺序仍是**先读输入框当前值**（包含刚改还没点「保存更正」的），
   读不到才退回库里下发的 d —— 否则复制出来的是改动之前的内容，容易发错。 */
function copyBasicInfo(){
  var d = CUR_DETAIL || {};
  function cur(id, fallback){
    var el = document.getElementById(id);
    return (el && el.value !== '' && el.value != null) ? el.value : (fallback || '');
  }
  function clean(v){ return (v == null ? '' : String(v)).trim(); }
  var name = clean(cur('edName', d.name));
  var gender = clean(cur('edGender', d.gender));
  var phone = clean(cur('edPhone', d.phone));
  var email = clean(cur('edEmail', d.email));
  if(phone === '—') phone = '';
  if(email === '—') email = '';

  var lines = [];
  /* 第一行：姓名 + 性别（中间用全角空格，粘到微信里是一行，好读也好删） */
  var head = [name, gender].filter(Boolean).join('　');
  if(head) lines.push(head);
  if(phone) lines.push(phone);
  if(email) lines.push(email);
  if(!lines.length){ toast('这个人还没有可复制的信息', 'warn'); return; }
  copyText(lines.join('\n'));
}

/* ---------- 档案 → 写邮件（2026-10-30 业务方要求） ----------
   点档案里的绿色「写邮件」：跳到写邮件页、把这个人的收件人填好，
   并且**默认选中「面试邀约」模板** —— 选模板会触发 renderMailVars()，
   而它末尾会调 autofillInterviewVars()，于是档案里填过的
   面试单位 / 时间 / 时段 / 方式自动带进邮件，HR 不用再敲一遍。 */
var MAIL_JUMP_CID = null;
function mailTplInterviewId(){
  /* 模板名/场景里带"面试"或"邀约"的第一个 —— 不写死 id，模板可以改名 */
  for(var i = 0; i < (MAIL_TPL || []).length; i++){
    var t = MAIL_TPL[i];
    var s = (t.name || '') + ' ' + (t.scene || '');
    if(s.indexOf('面试') >= 0 || s.indexOf('邀约') >= 0) return String(t.id);
  }
  return '';
}
function waitFor(cond, timeout){
  return new Promise(function(resolve){
    var t0 = Date.now();
    (function tick(){
      if(cond()) return resolve(true);
      if(Date.now() - t0 > (timeout || 4000)) return resolve(false);
      setTimeout(tick, 60);
    })();
  });
}
async function mailFor(cid){
  MAIL_JUMP_CID = cid;
  setView('mail');
  /* initMail 是异步的，setView 不 await 它 —— 等页面真正画出来再选人 */
  var ready = await waitFor(function(){ return !!document.getElementById('mCandInput'); });
  if(!ready){ toast('写邮件页没打开，请从左侧「写邮件」重试', 'warn'); return; }
  if(MAIL_JUMP_CID !== cid) return;        /* 中途又点了别人 → 以后来的为准 */
  await pickCand(cid);
  var sel = document.getElementById('mTpl');
  if(sel && !sel.value){
    var tid = mailTplInterviewId();
    if(tid){
      sel.value = tid;
      /* 手动派发 change：让"换模板 → 重画变量 → 带出面试信息 → 生成草稿"这条链跑起来 */
      sel.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }
  MAIL_JUMP_CID = null;
}
/* 助手草稿 → 写邮件页：选人、选模板、填主题/正文，避免跳过去是一片空白。 */
async function openDraftInMailer(d){
  if(!d) return;
  var cid = d.candidate_id;
  setView('mail');
  var ready = await waitFor(function(){ return !!document.getElementById('mCandInput'); }, 6000);
  if(!ready){ toast('写邮件页没打开，请从左侧「写邮件」重试', 'warn'); return; }
  if(cid) await pickCand(cid);
  await waitFor(function(){
    return !!document.getElementById('mSubject') && !!document.getElementById('mBody');
  }, 3000);

  var sel = document.getElementById('mTpl');
  if(sel && d.template){
    var opt = Array.prototype.filter.call(sel.options, function(o){
      return String(o.text || '').indexOf(d.template) >= 0;
    })[0];
    /* 只把模板选中，不 dispatch change：
       change 会异步向服务端再生成一版草稿，可能把下面填好的助手草稿覆盖掉。
       HR 之后自己点「生成草稿」时仍会按这个模板渲染。 */
    if(opt) sel.value = opt.value;
  }

  var name = d.name || '';
  var subj = String(d.subject || '');
  /* 只信服务端生成的 body_html；纯文本 fallback 必须转义后再进 innerHTML。 */
  var html = d.body_html || (d.body ? esc(d.body) : '');
  if(name){
    subj = subj.replace(/\{name\}|\{姓名\}/gi, name);
    /* html 是 HTML 片段：姓名必须转义后再替换，否则姓名里的标签会被执行。 */
    html = String(html).replace(/\{name\}|\{姓名\}/gi, esc(name));
  }
  var s = document.getElementById('mSubject');
  var b = document.getElementById('mBody');
  if(s) s.value = subj;
  if(b) b.innerHTML = html;
  if(d.to) M_TO = d.to;
  var sent = document.getElementById('mailSentBox');
  if(sent) sent.innerHTML = '';
  M_RESULT = null;
  toast('已填入草稿');
}
/* 复制按钮统一走 data-copy + 事件委托：
   不要把用户数据拼进 inline onclick —— HTML 转义（&#39;）在属性解析后会
   还原成单引号，提前结束 JS 字符串就是脚本注入。 */
document.addEventListener('click', function(ev){
  var el = (ev.target && ev.target.closest) ? ev.target.closest('[data-copy]') : null;
  if(el){ ev.preventDefault(); copyText(el.getAttribute('data-copy')); }
});
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
async function openSourceFile(name, inline){
  var w = inline ? window.open('', '_blank') : null;
  var url = await fileUrl('/api/sources/file?inline=' + (inline ? 1 : 0) +
    '&name=' + encodeURIComponent(name), 'source', {name: name, inline: !!inline});
  if(inline){
    if(w) w.location = url; else window.open(url, '_blank');
  }else{ var a=document.createElement('a'); a.href=url; a.download='';
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
    var rr = await fetch('/api/sources/bundle',
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
   登录模式下靠 `fileUrl()` 换短时票据（与打开简历原件同一口径）。 */
async function outboxOpen(oid, name){
  if(!oid) return;
  var w = window.open('', '_blank');
  var url = await fileUrl('/api/mail/outbox/' + encodeURIComponent(oid)
    + '?name=' + encodeURIComponent(name || ''), 'outbox',
    {id: oid, name: name || ''});
  if(w) w.location = url; else window.open(url, '_blank');
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
      var r = await fetch('/api/mail/outbox?name=' + encodeURIComponent(f.name),
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
  /* 用服务端生成的 body_html（纯文本模板已经转义）；r.body 是纯文本，
     直接塞 innerHTML 会把候选人姓名里的 <img onerror> 当 HTML 执行。 */
  document.getElementById('mBody').innerHTML = r.body_html || esc(r.body || '');
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
    <button class="btn-primary" onclick="insertTableDialog('${id}', this)">插入表格</button>
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

/* 插入表格：点工具栏后弹一个贴近按钮的小型 10×10 网格，
   鼠标划过预览行列，点一下直接插入。它不占用大弹窗，也不会覆盖模板编辑器。 */
var TABLE_PICK_TARGET = 'mBody';
var TABLE_PICK_RANGE = null;
var TABLE_PICK_ANCHOR = null;

function closeTablePicker(){
  var p = document.getElementById('tablePicker');
  if(p) p.remove();
}

function insertTableDialog(id, anchor){
  TABLE_PICK_TARGET = id || 'mBody';
  TABLE_PICK_ANCHOR = anchor || null;
  TABLE_PICK_RANGE = null;
  var ed = document.getElementById(TABLE_PICK_TARGET);
  if(ed){
    try{
      var sel = window.getSelection();
      if(sel && sel.rangeCount){
        var r = sel.getRangeAt(0);
        if(ed.contains(r.commonAncestorContainer)) TABLE_PICK_RANGE = r.cloneRange();
      }
    }catch(e){}
  }
  closeTablePicker();

  var p = document.createElement('div');
  p.id = 'tablePicker';
  p.className = 'tablepicker';
  var cells = '';
  for(var r = 1; r <= 10; r++){
    for(var c = 1; c <= 10; c++){
      cells += '<button type="button" class="tpc" data-r="' + r + '" data-c="' + c +
        '" aria-label="' + r + ' 行 ' + c + ' 列"></button>';
    }
  }
  p.innerHTML = '<div class="tplabel">插入表格 <span id="tpDim">2 × 2</span></div>' +
    '<div class="tpgrid">' + cells + '</div>' +
    '<div class="tphint">点格子即插入；最多 10×10，插完可用「＋行/＋列」继续扩展。</div>';
  document.body.appendChild(p);

  var dim = p.querySelector('#tpDim');
  var all = p.querySelectorAll('.tpc');
  function mark(rows, cols, commit){
    Array.prototype.forEach.call(all, function(x){
      var rr = Number(x.dataset.r), cc = Number(x.dataset.c);
      x.classList.toggle('on', rr <= rows && cc <= cols);
      x.classList.toggle('anchor', !!commit && rr === rows && cc === cols);
    });
    if(dim) dim.textContent = rows + ' × ' + cols;
  }
  Array.prototype.forEach.call(all, function(x){
    var rr = Number(x.dataset.r), cc = Number(x.dataset.c);
    x.addEventListener('mouseenter', function(){ mark(rr, cc, false); });
    x.addEventListener('click', function(ev){
      ev.preventDefault();
      ev.stopPropagation();
      insertPickedTable(rr, cc);
    });
  });
  mark(2, 2, false);

  var rect = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : null;
  var left = rect ? rect.left : (window.innerWidth - 240) / 2;
  var top = rect ? rect.bottom + 6 : 110;
  left = Math.max(8, Math.min(left, window.innerWidth - p.offsetWidth - 8));
  top = Math.max(8, Math.min(top, window.innerHeight - p.offsetHeight - 8));
  p.style.left = left + 'px';
  p.style.top = top + 'px';

  if(!window.__tablePickerBound){
    window.__tablePickerBound = true;
    document.addEventListener('pointerdown', function(ev){
      var picker = document.getElementById('tablePicker');
      if(!picker) return;
      if(picker.contains(ev.target) || ev.target === TABLE_PICK_ANCHOR) return;
      closeTablePicker();
    });
    document.addEventListener('keydown', function(ev){
      if(ev.key === 'Escape' && document.getElementById('tablePicker')){
        ev.stopPropagation();
        closeTablePicker();
      }
    }, true);
  }
}

function _tableHtml(rows, cols){
  const head = '<tr>' + Array.from({length: cols}, (_, c) =>
    `<th>${c === 0 ? '项目' : (c === 1 ? '内容' : '列' + (c + 1))}</th>`).join('') + '</tr>';
  const body = Array.from({length: Math.max(0, rows - 1)}, () =>
    '<tr>' + Array.from({length: cols}, () => '<td>&nbsp;</td>').join('') + '</tr>').join('');
  return `<table><thead>${head}</thead><tbody>${body}</tbody></table><p><br></p>`;
}

function insertPickedTable(rows, cols){
  var target = TABLE_PICK_TARGET;
  var range = TABLE_PICK_RANGE;
  closeTablePicker();
  TABLE_PICK_RANGE = null;
  var ed = _focusEditor(target);
  if(!ed) return;
  if(range){
    try{
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }catch(e){}
  }
  var html = _tableHtml(rows, cols);
  var done = false;
  try { done = document.execCommand('insertHTML', false, html); } catch(e){ done = false; }
  if(!done){
    var frag = document.createRange().createContextualFragment(html);
    if(range && ed.contains(range.commonAncestorContainer)){
      try{ range.deleteContents(); range.insertNode(frag); }
      catch(e){ ed.appendChild(frag); }
    } else {
      ed.appendChild(frag);
    }
  }
  beautifyTable(target);
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
/* 兼容旧入口：模板里插入表格也走统一的“选行列”弹窗，不再固定 2 列。 */
function tplInsertTable(){
  insertTableDialog('tplEditor');
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

document.getElementById('chkAll').addEventListener('change', async function(e){
  var on = !!e.target.checked;
  if(!on){ clearSel(); return; }
  /* 全选 = **当前筛选条件下的全部人（跨页）**，不再只选当前这一页。
     只取 id（ids_only）避免为了勾选把几千人的详情都装配一遍。 */
  var r = await api('/api/candidates' + query() + '&ids_only=1');
  if(r.__err || !r.ids){
    e.target.checked = false;
    toast((r && r.__err) || '全选失败，请重试', 'danger');
    return;
  }
  SEL = {};
  r.ids.forEach(function(id){ SEL[Number(id)] = true; });
  Array.prototype.forEach.call(document.querySelectorAll('#list .chk input'),
    function(c){ c.checked = !!SEL[Number(c.dataset.id)]; });
  syncSel();
  renderPager();
  toast('已选中当前筛选结果 ' + r.ids.length + ' 人（含其他页）');
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
(async function boot(){
  syncThemeBtn();
  /* 先按"当前还不知道是谁"收紧界面：账号管理入口藏起来、应用区压暗。
     登录成功后再由 renderWhoami() 放开。**不能等 loadMe() 回来才做** ——
     那一瞬间里侧栏是全亮的，看起来像没生效。 */
  applyPermissions();
  if(!AUTH_ENABLED){            /* 本机模式：默认进入智能助手 */
    applyUrlState();
    URL_READY = true;
    enterDefaultView();
    applyPermissions();
    await loadJobFilterOptions();
    load({push:false});
    /* 右侧默认展示「概览 · 待办」（业务方 2026-10-21）：没人选中时不该只留一句
       "左侧选一个人" —— 那块地方占着半屏，正好放今天该干什么。 */
    showOverview();
    restoreIngestState();
    return;
  }
  if(!TOKEN){ showLogin(''); return; }
  /* 有 token 但可能已失效：先问 /api/me。失效时 api() 会自动弹登录页，
     所以这里不需要额外的分支。 */
  loadMe().then(async function(){
    if(!ME){ showLogin('登录已失效，请重新登录'); return; }
    /* 统一走 bootAfterLogin：刷新页面时同样要处理"必须改初始密码"。 */
    await bootAfterLogin();
  });
})();

/* ============================================================
   全站单选下拉：应用内自绘弹层
   ------------------------------------------------------------
   原生 <select> 仍然保留（表单值和 change 事件都不变），只把它点开时
   系统画的蓝色列表换成与页面输入框同一套圆角/底色。事件用委托绑定，
   动态生成的 select 也自动生效；多选 / size>1 / disabled 不接管。
   ============================================================ */
var SEL_MENU = null;
function closeSelMenu(){
  if(SEL_MENU){ SEL_MENU.remove(); SEL_MENU = null; }
  document.removeEventListener('mousedown', selOutside, true);
  window.removeEventListener('scroll', selScroll, true);
  window.removeEventListener('resize', closeSelMenu);
}
function selOutside(ev){
  if(!SEL_MENU) return;
  if(SEL_MENU.contains(ev.target)) return;
  if(ev.target.closest && ev.target.closest('select')) return;
  closeSelMenu();
}
function selScroll(ev){
  if(SEL_MENU && ev.target && (ev.target === SEL_MENU || SEL_MENU.contains(ev.target))) return;
  closeSelMenu();
}
function openSelMenu(sel){
  if(!sel || sel.disabled || sel.multiple || sel.size > 1) return false;
  closeSelMenu();
  var opts = Array.prototype.slice.call(sel.options || []);
  if(!opts.length) return false;
  var menu = document.createElement('div');
  menu.className = 'selMenu';
  menu.setAttribute('role', 'listbox');
  menu.innerHTML = opts.map(function(o, i){
    var cls = 'selOpt' + (i === sel.selectedIndex ? ' on' : '') +
      ((o.value === '' && !o.text) || /^(请选择|选择一|不限|全部)/.test(o.text || '')
        ? ' placeholder' : '');
    return '<button type="button" class="' + cls + '" role="option" data-i="' + i + '"' +
      ' title="' + esc(o.text || '') + '"' +
      ' aria-selected="' + (i === sel.selectedIndex ? 'true' : 'false') + '">' +
      '<span>' + esc(o.text || '') + '</span><span class="selChk">✓</span></button>';
  }).join('');
  document.body.appendChild(menu);
  var rect = sel.getBoundingClientRect();
  var w = Math.min(Math.max(rect.width, 180), 360);
  menu.style.width = w + 'px';
  var mh = Math.min(menu.scrollHeight, 320);
  menu.style.maxHeight = mh + 'px';
  var left = Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - w - 8));
  var above = rect.top - mh - 6;
  var below = rect.bottom + 6;
  var top = (below + mh + 8 > window.innerHeight && above >= 8) ? above : below;
  menu.style.left = left + 'px';
  menu.style.top = Math.max(8, top) + 'px';
  SEL_MENU = menu;
  var cur = menu.querySelector('.selOpt.on') || menu.querySelector('.selOpt');
  if(cur){
    try{ cur.focus({preventScroll:true}); cur.scrollIntoView({block:'nearest'}); }
    catch(e){ cur.focus(); }
  }
  menu.addEventListener('mousedown', function(ev){ ev.preventDefault(); });
  menu.addEventListener('click', function(ev){
    var b = ev.target.closest ? ev.target.closest('.selOpt') : null;
    if(!b) return;
    var i = Number(b.dataset.i);
    if(sel.options[i]){
      sel.selectedIndex = i;
      sel.dispatchEvent(new Event('input', {bubbles:true}));
      sel.dispatchEvent(new Event('change', {bubbles:true}));
    }
    closeSelMenu();
    try{ sel.focus({preventScroll:true}); }catch(e){}
  });
  menu.addEventListener('keydown', function(ev){
    var all = Array.prototype.slice.call(menu.querySelectorAll('.selOpt'));
    var at = all.indexOf(document.activeElement);
    if(ev.key === 'ArrowDown' || ev.key === 'ArrowUp'){
      ev.preventDefault();
      var next = ev.key === 'ArrowDown'
        ? all[Math.min(all.length - 1, at + 1)] : all[Math.max(0, at - 1)];
      if(next) next.focus();
    } else if(ev.key === 'Enter' || ev.key === ' '){
      ev.preventDefault();
      if(document.activeElement.classList.contains('selOpt')) document.activeElement.click();
    }
  });
  setTimeout(function(){
    document.addEventListener('mousedown', selOutside, true);
    window.addEventListener('scroll', selScroll, true);
    window.addEventListener('resize', closeSelMenu);
  }, 0);
  return true;
}
document.addEventListener('mousedown', function(ev){
  if(ev.button !== undefined && ev.button !== 0) return;
  var sel = ev.target && ev.target.closest ? ev.target.closest('select') : null;
  if(!sel || sel.disabled || sel.multiple || sel.size > 1) return;
  var ok = false;
  try{ ok = openSelMenu(sel); }catch(e){ closeSelMenu(); }
  if(ok) ev.preventDefault();
}, true);
document.addEventListener('keydown', function(ev){
  if(ev.key === 'Escape'){ closeSelMenu(); return; }
  var sel = ev.target;
  if(!sel || sel.tagName !== 'SELECT' || sel.multiple || sel.disabled || sel.size > 1) return;
  if(ev.key === 'Enter' || ev.key === ' ' || ev.key === 'ArrowDown'){
    ev.preventDefault();
    try{ openSelMenu(sel); }catch(e){ closeSelMenu(); }
  }
});
"""
