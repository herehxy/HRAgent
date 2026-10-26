// 弹窗与输入框改版的浏览器端验收（CDP 驱动真实 Edge）
// 用法：node tools/_ui_modal_check.js <url> <outDir>
// 判据两边都验：改前应失败、改后应通过。脚本本身不 mock 任何数据。
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

function httpJson(url) {
  return fetch(url).then(r => r.json());
}

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const events = [];
  ws.addEventListener('message', ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
    } else if (msg.method) {
      events.push(msg);
    }
  });
  const ready = new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', rej);
  });
  return {
    ready, events,
    send(method, params) {
      const mid = ++id;
      return new Promise((res, rej) => {
        pending.set(mid, { res, rej });
        ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
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
    throw new Error('JS 异常：' + (r.exceptionDetails.text || '') +
      ' ' + (r.exceptionDetails.exception && r.exceptionDetails.exception.description || ''));
  }
  return r.result.value;
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });

  // 找一个页面 target
  let targets = await httpJson(CDP + '/json/list');
  let page = targets.find(t => t.type === 'page');
  if (!page) {
    // 新开一个
    page = await httpJson(CDP + '/json/new?about:blank');
  }
  const c = connect(page.webSocketDebuggerUrl);
  await c.ready;
  await c.send('Page.enable');
  await c.send('Runtime.enable');
  // headless 下页面默认无系统焦点，`:focus` 不匹配 → 聚焦类判据会误报
  await c.send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 900, deviceScaleFactor: 1, mobile: false
  });

  console.log('打开：' + URL_);
  await c.send('Page.navigate', { url: URL_ });
  await new Promise(r => setTimeout(r, 3500));

  // ---------------------------------------------------------------- 1. 结构
  console.log('\n=== 1. 弹窗结构与无障碍属性 ===');
  const struct = await evalJs(c, `(() => {
    const m = document.getElementById('modal');
    const s = document.getElementById('mSheet');
    const cs = getComputedStyle(s);
    const t = document.getElementById('mTitle');
    return {
      role: m && m.getAttribute('role'),
      ariaModal: m && m.getAttribute('aria-modal'),
      labelledby: m && m.getAttribute('aria-labelledby'),
      labelledByExists: !!(m && m.getAttribute('aria-labelledby') &&
                           document.getElementById(m.getAttribute('aria-labelledby'))),
      tabindex: m && m.getAttribute('tabindex'),
      overflow: cs.overflow,
      radius: cs.borderRadius,
      shadow: cs.boxShadow,
      borderW: cs.borderTopWidth,
      hasBd: !!document.getElementById('mBody'),
      bdOverflow: document.getElementById('mBody') && getComputedStyle(document.getElementById('mBody')).overflowY,
      bdMinH: document.getElementById('mBody') && getComputedStyle(document.getElementById('mBody')).minHeight,
      hasFoot: !!document.getElementById('mFoot'),
      closeAria: (() => { const b=[...m.querySelectorAll('button')].find(x=>x.getAttribute('aria-label')==='关闭');
                          return b ? b.getAttribute('aria-label') : null; })(),
      sizeClasses: ['sm','md','lg','xl'].filter(k =>
        [...document.styleSheets].some(ss => { try {
          return [...ss.cssRules].some(r => r.selectorText && r.selectorText.includes('.sheet--'+k));
        } catch(e){ return false; } }))
    };
  })()`);
  ok(struct.role === 'dialog', '容器 role="dialog"', struct.role);
  ok(struct.ariaModal === 'true', 'aria-modal="true"', struct.ariaModal);
  ok(struct.labelledby === 'mTitle' && struct.labelledByExists,
     'aria-labelledby 指向存在的标题', struct.labelledby);
  ok(struct.tabindex === '-1', '容器 tabindex="-1"（可聚焦兜底）', struct.tabindex);
  ok(struct.overflow === 'hidden', '容器 overflow:hidden（标题不跟滚）', struct.overflow);
  ok(struct.bdOverflow === 'auto', '正文区独立滚动', struct.bdOverflow);
  ok(struct.bdMinH === '0px', '正文区 min-height:0（flex 才能收缩）', struct.bdMinH);
  ok(!!struct.hasFoot, '存在独立底栏 #mFoot');
  ok(struct.closeAria === '关闭', '关闭按钮有 aria-label', struct.closeAria);
  ok(struct.sizeClasses.length === 4, '四档尺寸齐备',
     struct.sizeClasses.join('/'));
  ok(struct.borderW === '0px', '容器无边框（靠阴影浮起）', struct.borderW);
  ok(/rgba?\(31, 35, 41/.test(struct.shadow || ''), '双层阴影就位',
     (struct.shadow || '').slice(0, 40));

  // ---------------------------------------------------------------- 2. 输入框
  console.log('\n=== 2. 输入框：去框线改填充底 ===');
  /* 用注入节点的计算样式来量，不依赖"首屏恰好有输入框"——
     v1 首页确实没有文本框，旧写法会在这里假失败（问题在判据不在样式）。 */
  const inp = await evalJs(c, `(() => {
    const el = document.createElement('input');
    el.style.cssText = 'position:absolute;left:-9999px;top:0';
    document.body.appendChild(el);
    const cs = getComputedStyle(el);
    const out = {border: cs.borderTopWidth + ' ' + cs.borderTopStyle + ' ' + cs.borderTopColor,
                 bg: cs.backgroundColor, radius: cs.borderRadius};
    el.remove();
    return out;
  })()`);
  if (!inp) {
    ok(false, '量不到输入框样式');
  } else {
    ok(inp.border.startsWith('0px') || /transparent|rgba\(0, 0, 0, 0\)/.test(inp.border),
       '输入框无可见边框', inp.border);
    ok(/242, 243, 245/.test(inp.bg), '输入框是浅填充底 #f2f3f5', inp.bg);
  }

  // ---------------------------------------------------------------- 3. 交互
  console.log('\n=== 3. 焦点陷阱 / Esc / 焦点归还 ===');
  // 先造一个触发按钮，模拟"打开前的焦点元素"
  const seed = await evalJs(c, `(() => {
    const b = document.createElement('button');
    b.id = '__probeBtn'; b.textContent = 'probe';
    b.style.cssText = 'position:fixed;left:8px;top:8px;z-index:9999';
    document.body.appendChild(b);
    b.focus();
    return document.activeElement && document.activeElement.id;
  })()`);
  await evalJs(c, `openModal({size:'md'})`);
  await new Promise(r => setTimeout(r, 400));
  const focusInside = await evalJs(c, `(() => {
    const m = document.getElementById('modal');
    return {inside: m.contains(document.activeElement),
            tag: document.activeElement && document.activeElement.tagName};
  })()`);
  ok(focusInside.inside, '打开后焦点在弹窗内', focusInside.tag);

  // Tab 循环 25 次，看是否跑出弹窗
  await c.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
  await c.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
  let escaped = false;
  for (let i = 0; i < 25; i++) {
    await c.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
    await c.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
    const inside = await evalJs(c, `document.getElementById('modal').contains(document.activeElement)`);
    if (!inside) { escaped = true; break; }
  }
  ok(!escaped, '连按 Tab 25 次焦点未逃离弹窗');

  // data-lock 生效：锁住时 Esc 不关
  await evalJs(c, `openModal({size:'md', lock:true})`);
  await new Promise(r => setTimeout(r, 250));
  await c.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await c.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await new Promise(r => setTimeout(r, 300));
  const stillOpen = await evalJs(c, `document.getElementById('modal').classList.contains('on')`);
  ok(stillOpen, '门控型（lock）时 Esc 不关闭');

  // 解锁后 Esc 关闭
  await evalJs(c, `openModal({size:'md'})`);
  await new Promise(r => setTimeout(r, 250));
  await c.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await c.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await new Promise(r => setTimeout(r, 400));
  const closed = await evalJs(c, `document.getElementById('modal').classList.contains('on')`);
  ok(!closed, 'Esc 关闭弹窗');

  const returnFocus = await evalJs(c, `document.activeElement && document.activeElement.id`);
  ok(returnFocus === '__probeBtn', '关闭后焦点归还触发元素', String(returnFocus));

  // ---------------------------------------------------------------- 4. 尺寸
  console.log('\n=== 4. 尺寸分档实际生效 ===');
  const widths = {};
  for (const k of ['sm', 'md', 'lg', 'xl']) {
    await evalJs(c, `openModal({size:'${k}'})`);
    await new Promise(r => setTimeout(r, 220));
    widths[k] = await evalJs(c, `Math.round(document.getElementById('mSheet').getBoundingClientRect().width)`);
  }
  await evalJs(c, `closeModal()`);
  console.log('    实测宽度：', JSON.stringify(widths));
  ok(widths.sm === 400 && widths.md === 520 && widths.lg === 720 && widths.xl === 920,
     '四档宽度符合 400/520/720/920', `${widths.sm}/${widths.md}/${widths.lg}/${widths.xl}`);

  // ---------------------------------------------------------------- 5. 截图
  console.log('\n=== 5. 截图 ===');
  await evalJs(c, `(() => { const b=document.getElementById('__probeBtn'); if(b) b.remove(); })()`);
  // 5.1 人才库列表（看输入框观感）
  await evalJs(c, `refresh()`);
  await new Promise(r => setTimeout(r, 1200));
  let shot = await c.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, '01-list.png'), Buffer.from(shot.data, 'base64'));

  // 5.2 打开结果弹窗（中档）
  await evalJs(c, `resultModal({title:'导入完成', kind:'ok', headline:'导入成功',
      body:'共 12 份简历：新增 9 份，重复 3 份（已跳过）。\\n源：待导入文件夹'})`);
  await new Promise(r => setTimeout(r, 500));
  shot = await c.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, '02-modal-md.png'), Buffer.from(shot.data, 'base64'));

  // 5.3 进度弹窗（门控型 + 底栏按钮）
  await evalJs(c, `progressModal('__probe__', {title:'正在导入', desc:'正在解析简历并入库', stoppable:true})`);
  await new Promise(r => setTimeout(r, 500));
  shot = await c.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, '03-modal-progress.png'), Buffer.from(shot.data, 'base64'));
  await evalJs(c, `closeModal()`);

  // 5.4 窄屏：底部面板降级
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: 420, height: 820, deviceScaleFactor: 1, mobile: true
  });
  await evalJs(c, `resultModal({title:'导入完成', kind:'warn', headline:'部分文件未上传成功',
      body:'共 3 份：成功 1 份，失败 2 份'})`);
  await new Promise(r => setTimeout(r, 600));
  shot = await c.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, '04-bottom-sheet.png'), Buffer.from(shot.data, 'base64'));
  const bs = await evalJs(c, `(() => {
    const s = document.getElementById('mSheet');
    const cs = getComputedStyle(s);
    const m = document.getElementById('modal');
    return {radius: cs.borderTopLeftRadius, radiusBottom: cs.borderBottomLeftRadius,
            align: getComputedStyle(m).alignItems,
            w: Math.round(s.getBoundingClientRect().width),
            vw: window.innerWidth};
  })()`);
  ok(bs.radius === '16px' && bs.radiusBottom === '0px',
     '窄屏弹窗只圆上面两角', `${bs.radius}/${bs.radiusBottom}`);
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
