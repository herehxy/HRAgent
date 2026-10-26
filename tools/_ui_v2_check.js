// 弹窗与输入框改版的浏览器端验收（CDP 驱动真实 Edge）
// 针对 **v2 主界面**（app/ui_v2.py）：#mask / #dlg / #dlgTitle / #dlgBody / #dbar
// 用法：node tools/_ui_v2_check.js <url> <outDir>
// 判据两边都验：改前应失败、改后应通过。不 mock 任何数据，走真实接口。
const fs = require('fs');
const path = require('path');

const URL_ = process.argv[2] || 'http://127.0.0.1:8791/';
const OUT = process.argv[3] || path.join(process.cwd(), 'out', 'ui-check');
const CDP = 'http://127.0.0.1:9334';

const PASS = [], FAIL = [];
function ok(cond, name, extra) {
  (cond ? PASS : FAIL).push(name);
  console.log(`  [${cond ? '√' : '×'}] ${name}${extra ? '　' + extra : ''}`);
  return !!cond;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
const httpJson = url => fetch(url).then(r => r.json());

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
    }
  });
  const ready = new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', rej);
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

async function evalJs(c, expr) {
  const r = await c.send('Runtime.evaluate', {
    expression: expr, returnByValue: true, awaitPromise: true
  });
  if (r.exceptionDetails) {
    throw new Error('JS 异常：' + (r.exceptionDetails.text || '') + ' ' +
      ((r.exceptionDetails.exception && r.exceptionDetails.exception.description) || ''));
  }
  return r.result.value;
}

async function key(c, k, code, vk) {
  await c.send('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code, windowsVirtualKeyCode: vk });
  await c.send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code, windowsVirtualKeyCode: vk });
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  let targets = await httpJson(CDP + '/json/list');
  let page = targets.find(t => t.type === 'page');
  if (!page) page = await httpJson(CDP + '/json/new?about:blank');
  const c = connect(page.webSocketDebuggerUrl);
  await c.ready;
  await c.send('Page.enable');
  await c.send('Runtime.enable');
  /* headless 下页面默认"没有系统焦点"，`:focus` 伪类不会匹配 ——
     不打开这个开关，所有聚焦态判据都会拿到未聚焦的样式（假 FAIL）。
     这不是页面 bug，是测试环境需要显式模拟焦点。 */
  await c.send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 900, deviceScaleFactor: 1, mobile: false
  });

  console.log('打开：' + URL_);
  await c.send('Page.navigate', { url: URL_ });
  await sleep(3800);

  const isV2 = await evalJs(c, `!!document.getElementById('dlg')`);
  ok(isV2, '页面是 v2 主界面（存在 #dlg）');
  if (!isV2) { console.log('不是 v2 页面，退出'); process.exit(2); }

  // ---------------------------------------------------------------- 1. 结构
  console.log('\n=== 1. 弹窗结构与无障碍属性 ===');
  const st = await evalJs(c, `(() => {
    const mask = document.getElementById('mask');
    const dlg  = document.getElementById('dlg');
    const cs   = getComputedStyle(dlg);
    const t    = document.getElementById('dlgTitle');
    const body = document.getElementById('dlgBody');
    const bar  = document.querySelector('#mask .dbar');
    return {
      role: dlg.getAttribute('role'),
      ariaModal: dlg.getAttribute('aria-modal'),
      labelledby: dlg.getAttribute('aria-labelledby'),
      labelledOk: !!(dlg.getAttribute('aria-labelledby') &&
                     document.getElementById(dlg.getAttribute('aria-labelledby'))),
      tabindex: dlg.getAttribute('tabindex'),
      overflow: cs.overflow,
      radius: cs.borderTopLeftRadius,
      shadow: cs.boxShadow,
      borderW: cs.borderTopWidth,
      bodyOverflow: getComputedStyle(body).overflowY,
      bodyMinH: getComputedStyle(body).minHeight,
      barFlex: bar && getComputedStyle(bar).flex,
      sizeClasses: ['sm','md','lg','xl'].filter(k => {
        const el = document.createElement('div');
        el.className = 'dlg ' + k;
        el.style.cssText = 'position:absolute;left:-9999px';
        document.body.appendChild(el);
        const w = getComputedStyle(el).width;
        el.remove();
        return w && w !== 'auto' && w !== '0px';
      })
    };
  })()`);
  ok(st.role === 'dialog', '容器 role="dialog"', st.role);
  ok(st.ariaModal === 'true', 'aria-modal="true"', st.ariaModal);
  ok(st.labelledby === 'dlgTitle' && st.labelledOk,
     'aria-labelledby 指向存在的标题', st.labelledby);
  ok(st.tabindex === '-1', '容器 tabindex="-1"（可聚焦兜底）', st.tabindex);
  ok(st.overflow === 'hidden', '容器 overflow:hidden（标题不跟滚）', st.overflow);
  ok(st.bodyOverflow === 'auto', '正文区独立滚动', st.bodyOverflow);
  ok(st.bodyMinH === '0px', '正文区 min-height:0（flex 才能收缩）', st.bodyMinH);
  ok(st.barFlex === '0 0 auto' || st.barFlex === 'none',
     '底栏 flex:none（固定不滚）', st.barFlex);
  ok(st.sizeClasses.length === 4, '四档尺寸齐备', st.sizeClasses.join('/'));
  ok(st.borderW === '0px', '容器无边框（靠阴影浮起）', st.borderW);
  ok(/rgba?\(31, 35, 41/.test(st.shadow || ''), '阴影就位', (st.shadow || '').slice(0, 46));

  // ---------------------------------------------------------------- 2. 输入框
  console.log('\n=== 2. 输入框：去框线改填充底 ===');
  // 列表页首屏没有表单控件，先打开"按姓名检索"这类带输入的弹窗再量。
  // 用 askText 造一个：它就是一个真实业务形态（重命名/新建都用它）。
  // ⚠️ 必须 `void` 掉：askText 返回 Promise（等用户点确定），
  // 而 evalJs 用 awaitPromise:true → 直接 await 会永远挂住，整个脚本超时。
  await evalJs(c, `void askText('测试输入', '初始值', '占位'); 1`);
  await sleep(450);
  // 聚焦前先 blur，并**等过渡走完**再测静止态。
  // 两步都必须做：askText 打开时自动聚焦（不 blur 测的是 focus 态）；
  // 而 background-color 有 .14s 过渡（blur 后立刻读会拿到渐变中的中间值，
  // 实测报成 rgb(255,255,255) 白底 → 误判"没有填充底"）。
  await evalJs(c, `(() => { const el = document.getElementById('dlgInput'); if (el) el.blur(); return 1; })()`);
  await sleep(320);
  const inp = await evalJs(c, `(() => {
    const el = document.getElementById('dlgInput') ||
               document.querySelector('.cinput input') ||
               document.querySelector('.crow input');
    if (!el) return {none: true};
    el.blur();
    const cs = getComputedStyle(el);
    return {border: cs.borderTopWidth + ' ' + cs.borderTopStyle + ' ' + cs.borderTopColor,
            bg: cs.backgroundColor, id: el.id || el.className};
  })()`);
  if (inp.none) {
    ok(false, '弹窗里找不到输入框');
  } else {
    /* 「无可见边框」的正确判据：宽度 0，或颜色全透明。
       浏览器把 transparent 算成 rgba(0, 0, 0, 0) —— 只认字面量 transparent
       会误报（实测踩到）。 */
    const invisible = inp.border.startsWith('0px') ||
                      /transparent|rgba\(0, 0, 0, 0\)/.test(inp.border);
    ok(invisible, '输入框无可见边框', inp.border + ' (' + inp.id + ')');
    ok(/242, 243, 245/.test(inp.bg), '输入框是浅填充底 #f2f3f5（静止态）', inp.bg);
  }
  // 聚焦后（同样要等 .14s 过渡走完）再量：底色转白 + 柔光，而不是 2px 深蓝硬边
  await evalJs(c, `(() => { const el = document.getElementById('dlgInput'); if (el) el.focus(); return 1; })()`);
  await sleep(320);
  const foc = await evalJs(c, `(() => {
    const el = document.getElementById('dlgInput');
    if (!el) return {none: true};
    const cs = getComputedStyle(el);
    /* ⚠️ 判"有没有硬 outline"要看 outlineStyle，不能看 outlineWidth：
       CSS 规范里 outline-style:none 时 outline-width 的计算值仍是 medium(3px)，
       拿宽度当判据会恒真/恒假（实测误报成 3px 硬圈）。 */
    return {bg: cs.backgroundColor, shadow: cs.boxShadow, style: cs.outlineStyle};
  })()`);
  if (!foc.none) {
    ok(/255, 255, 255/.test(foc.bg), '聚焦时底色转白', foc.bg);
    ok(/rgba?\(51, 112, 255/.test(foc.shadow || ''), '聚焦是柔光不是硬边框',
       (foc.shadow || '').slice(0, 40));
    ok(foc.style === 'none', '聚焦不使用 outline 硬圈', foc.style);
  }
  /* ⚠️ 覆盖面要补：`.cinput input` / `.crow input` 与 `.dlg-input` 是**三处独立规则**，
     只测弹窗里那个输入框会漏掉表单里真正大量使用的另外两处（实测：把
     .cinput 的 border 改回 var(--line-2) 时，只看 .dlg-input 的判据全绿，漏报）。
     这里注入一个临时节点量真实计算样式 —— CSS 规则对任何同结构元素都生效，
     不依赖"页面上恰好有表单"这种偶然条件。 */
  const formInp = await evalJs(c, `(() => {
    const wrap = document.createElement('div');
    wrap.className = 'cinput';
    wrap.style.cssText = 'position:absolute;left:-9999px;top:0';
    const el = document.createElement('input');
    wrap.appendChild(el); document.body.appendChild(wrap);
    const cs = getComputedStyle(el);
    const out = {border: cs.borderTopColor, bw: cs.borderTopWidth, bg: cs.backgroundColor};
    wrap.remove();
    return out;
  })()`);
  const formInvisible = formInp.bw === '0px' ||
                        /transparent|rgba\(0, 0, 0, 0\)/.test(formInp.border);
  ok(formInvisible, '表单输入框（.cinput）也无可见边框',
     formInp.bw + ' ' + formInp.border);
  ok(/242, 243, 245/.test(formInp.bg), '表单输入框（.cinput）是浅填充底', formInp.bg);

  /* 输入框测试会往 #mask 里塞内容、改焦点，**重新加载页面**把状态清干净，
     否则后面的焦点与焦点归还判据会受污染（实测：不刷新时 activeElement
     还停在上一步的探针按钮上）。 */
  await c.send('Page.navigate', { url: URL_ });
  await sleep(3200);

  // ---------------------------------------------------------------- 3. 交互
  console.log('\n=== 3. 焦点陷阱 / Esc / 焦点归还 ===');
  await evalJs(c, `(() => {
    const b = document.createElement('button');
    b.id = '__probeBtn'; b.textContent = 'probe';
    b.style.cssText = 'position:fixed;left:8px;top:8px;z-index:9999';
    document.body.appendChild(b); b.focus();
  })()`);

  await evalJs(c, `resultModal({title:'导入完成', kind:'ok', headline:'导入成功', body:'共 12 份简历。'})`);
  await sleep(450);
  const f1 = await evalJs(c, `(() => {
    const mask = document.getElementById('mask');
    return {inside: mask.contains(document.activeElement),
            tag: document.activeElement && (document.activeElement.className || document.activeElement.tagName)};
  })()`);
  ok(f1.inside, '打开后焦点在弹窗内', f1.tag);

  let escaped = false;
  for (let i = 0; i < 12; i++) {
    await key(c, 'Tab', 'Tab', 9);
    const inside = await evalJs(c, `document.getElementById('mask').contains(document.activeElement)`);
    if (!inside) { escaped = true; break; }
  }
  ok(!escaped, '连按 Tab 12 次焦点未逃离弹窗');

  // lock：门控型不响应 Esc
  await evalJs(c, `openDlg({size:'md', lock:true})`);
  await sleep(250);
  await key(c, 'Escape', 'Escape', 27);
  await sleep(320);
  ok(await evalJs(c, `document.getElementById('mask').classList.contains('on')`),
     '门控型（lock）时 Esc 不关闭');

  // 解锁后 Esc 关闭
  await evalJs(c, `openDlg({size:'md'})`);
  await sleep(250);
  await key(c, 'Escape', 'Escape', 27);
  await sleep(450);
  ok(!(await evalJs(c, `document.getElementById('mask').classList.contains('on')`)),
     'Esc 关闭弹窗');

  const back = await evalJs(c, `document.activeElement && document.activeElement.id`);
  ok(back === '__probeBtn', '关闭后焦点归还触发元素', String(back));

  // ---------------------------------------------------------------- 4. 尺寸
  console.log('\n=== 4. 尺寸分档实际生效 ===');
  const widths = {};
  for (const k of ['sm', 'md', 'lg', 'xl']) {
    await evalJs(c, `openDlg({size:'${k}'})`);
    await sleep(220);
    widths[k] = await evalJs(c, `Math.round(document.getElementById('dlg').getBoundingClientRect().width)`);
  }
  await evalJs(c, `closeProgDlg()`);
  await sleep(250);
  console.log('    实测宽度：', JSON.stringify(widths));
  ok(widths.sm === 400 && widths.md === 520 && widths.lg === 720 && widths.xl === 940,
     '四档宽度符合 400/520/720/940',
     `${widths.sm}/${widths.md}/${widths.lg}/${widths.xl}`);

  // ---------------------------------------------------------------- 5. 截图
  console.log('\n=== 5. 截图 ===');
  await evalJs(c, `(() => { const b=document.getElementById('__probeBtn'); if(b) b.remove(); })()`);
  await sleep(300);
  let shot = await c.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, 'v2-01-list.png'), Buffer.from(shot.data, 'base64'));

  await evalJs(c, `resultModal({title:'导入完成', kind:'ok', headline:'导入成功',
      body:'共 12 份简历：新增 9 份，重复 3 份（已跳过）。'})`);
  await sleep(550);
  shot = await c.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, 'v2-02-modal-md.png'), Buffer.from(shot.data, 'base64'));

  await evalJs(c, `resultModal({title:'上传完成', kind:'warn', size:'lg',
      headline:'部分文件未上传成功', body:'共 3 份：成功 1 份，失败 2 份'})`);
  await sleep(500);
  shot = await c.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, 'v2-03-modal-lg.png'), Buffer.from(shot.data, 'base64'));
  await evalJs(c, `closeProgDlg()`);
  await sleep(300);

  // 窄屏
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: 420, height: 820, deviceScaleFactor: 1, mobile: true
  });
  await evalJs(c, `resultModal({title:'导入完成', kind:'warn',
      headline:'部分文件未上传成功', body:'共 3 份：成功 1 份，失败 2 份'})`);
  await sleep(600);
  shot = await c.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, 'v2-04-bottom-sheet.png'), Buffer.from(shot.data, 'base64'));
  const bs = await evalJs(c, `(() => {
    const d = document.getElementById('dlg');
    const cs = getComputedStyle(d);
    return {rTL: cs.borderTopLeftRadius, rBL: cs.borderBottomLeftRadius,
            align: getComputedStyle(document.getElementById('mask')).alignItems,
            w: Math.round(d.getBoundingClientRect().width), vw: window.innerWidth};
  })()`);
  ok(bs.rTL === '16px' && bs.rBL === '0px', '窄屏只圆上面两角', `${bs.rTL}/${bs.rBL}`);
  ok(bs.align === 'flex-end', '窄屏贴底对齐', bs.align);
  ok(bs.w === bs.vw, '窄屏撑满宽度', `${bs.w}/${bs.vw}`);

  await c.send('Emulation.clearDeviceMetricsOverride');
  console.log('\n' + '='.repeat(60));
  console.log(`通过 ${PASS.length} 项，失败 ${FAIL.length} 项`);
  FAIL.forEach(f => console.log('  FAIL: ' + f));
  console.log('截图：' + OUT);
  console.log('='.repeat(60));
  c.close();
  process.exit(FAIL.length ? 1 : 0);
}

main().catch(e => { console.error('脚本失败：', e); process.exit(2); });
