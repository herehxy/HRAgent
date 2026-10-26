// 真实 computedStyle 验收：所有输入控件必须"无可见边框 + 填充底"
// 判据不能看源码里有没有旧声明（旧规则仍在文件里，只是被后面的覆盖了），
// 只能量浏览器**实际生效**的值。
// 用法：node tools/_ui_inputs_check.js <url>
const CDP = 'http://127.0.0.1:9334';
const URL_ = process.argv[2] || 'http://127.0.0.1:8791/';

const PASS = [], FAIL = [];
function ok(cond, name, extra) {
  (cond ? PASS : FAIL).push(name);
  console.log(`  [${cond ? '√' : '×'}] ${name}${extra ? '　' + extra : ''}`);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0; const pending = new Map();
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id); pending.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
    }
  });
  const ready = new Promise((res, rej) => {
    ws.addEventListener('open', res); ws.addEventListener('error', rej);
  });
  return {
    ready,
    send(method, params) {
      const mid = ++id;
      return new Promise((res, rej) => {
        pending.set(mid, { res, rej });
        ws.send(JSON.stringify({ id: mid, method: method, params: params || {} }));
      });
    },
    close() { ws.close(); }
  };
}

(async () => {
  const list = await fetch(CDP + '/json/list').then(r => r.json());
  let page = list.find(t => t.type === 'page');
  if (!page) page = await fetch(CDP + '/json/new?about:blank').then(r => r.json());
  const c = connect(page.webSocketDebuggerUrl);
  await c.ready;
  await c.send('Page.enable');
  await c.send('Runtime.enable');
  await c.send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: 1600, height: 950, deviceScaleFactor: 1, mobile: false
  });
  await c.send('Page.navigate', { url: URL_ });
  await sleep(4000);

  const ev = async expr => {
    const r = await c.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
    return r.result.value;
  };

  // 关键：逐类控件量实际计算样式
  const res = await ev(`(() => {
    /* 页面上没出现的控件：注入一个**同结构**节点来量。
       CSS 规则对任何匹配的元素都生效，所以这个量法等价于真实控件；
       不这么做就只能验到首屏恰好存在的那 3 个（其余全靠猜）。 */
    const CASES = [
      ['顶部搜索框',        '<input class="search">'],
      ['筛选下拉 .sel',     '<select class="sel"></select>'],
      ['筛选下拉 .vsel',    '<select class="vsel"></select>'],
      ['阶段下拉 .stagesel','<select class="stagesel"></select>'],
      ['跳页框 .pgjump',    '<input class="pgjump">'],
      ['聊天输入 .chatinput textarea', '<div class="chatinput"><textarea></textarea></div>'],
      ['可编辑值 .cval .ced', '<div class="cval"><input class="ced"></div>'],
      ['多行表单 .mrow input', '<div class="mrow"><input></div>'],
      ['教育字段 .edu-field input', '<div class="edu-field"><input></div>'],
      ['面试单位 #madd_ivunit', '<select id="madd_ivunit"></select>'],
      ['表单字段 .cinput input', '<div class="cinput"><input></div>'],
      ['档案字段 .crow input',   '<div class="crow"><input></div>']
    ];
    const host = document.createElement('div');
    host.style.cssText = 'position:absolute;left:-9999px;top:0;width:600px';
    document.body.appendChild(host);
    const out = [];
    for (const [label, html] of CASES) {
      host.innerHTML = html;
      const sel = label.includes('.') ? null : null;
      const el = host.querySelector('input,select,textarea');
      if (!el) { out.push({label, missing: true}); continue; }
      const cs = getComputedStyle(el);
      out.push({
        label,
        bw: cs.borderTopWidth,
        color: cs.borderTopColor,
        bg: cs.backgroundColor
      });
    }
    host.remove();
    return out;
  })()`);

  console.log('=== 各输入控件的实际计算样式 ===');
  const invisible = r => r.bw === '0px' || /transparent|rgba\(0, 0, 0, 0\)/.test(r.color);
  const isField = bg => /242, 243, 245/.test(bg);

  for (const r of res) {
    if (r.missing) { console.log(`  [–] ${r.label}（页面上不存在，跳过）`); continue; }
    const good = invisible(r) && isField(r.bg);
    console.log(`  ${good ? '√' : '×'} ${r.label}: border=${r.bw} ${r.color} | bg=${r.bg}`);
    ok(invisible(r), `${r.label} 无可见边框`, `${r.bw} ${r.color}`);
    ok(isField(r.bg), `${r.label} 填充底 #f2f3f5`, r.bg);
  }

  // 富文本区：应当是白底 + 极浅边（不是灰底）
  const rich = await ev(`(() => {
    const el = document.getElementById('mBody');
    if (!el) return null;
    const cs = getComputedStyle(el);
    return {bw: cs.borderTopWidth, color: cs.borderTopColor, bg: cs.backgroundColor};
  })()`);
  if (rich) {
    console.log(`\n  富文本 #mBody: border=${rich.bw} ${rich.color} | bg=${rich.bg}`);
    ok(/255, 255, 255/.test(rich.bg), '富文本区保持白底（"纸"的观感）', rich.bg);
    ok(/222, 224, 227/.test(rich.color), '富文本区用极浅边 --line', rich.color);
  }

  console.log('\n' + '='.repeat(56));
  console.log(`通过 ${PASS.length} 项，失败 ${FAIL.length} 项`);
  FAIL.forEach(f => console.log('  FAIL: ' + f));
  console.log('='.repeat(56));
  c.close();
  process.exit(FAIL.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
