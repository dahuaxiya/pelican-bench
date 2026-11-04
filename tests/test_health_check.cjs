const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');

/** Drain asynchronous UI updates without firing polling timers automatically. */
async function settle() { for (let n = 0; n < 12; n++) { await new Promise(resolve => setImmediate(resolve)); } }

/** Load actual markup and health UI against fake reports; no official endpoint is called. */
function page() {
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'static/index.html'), 'utf8'), { url: 'http://127.0.0.1:8780/#health', runScripts: 'outside-only' });
  const w = dom.window, calls = [], timers = [];
  const a = { id: 'a'.repeat(32), environment: 'company', model: 'gpt-test', model_check: true, repeats: 1,
    protocol: 'codex-health-v1', started_at: 1792340000, status: 'completed', phase: '检查已结束', auth_source: 'site', cli_version: '1.0',
    route: { cli: {}, probe: {} }, route_changed: false,
    verdict: { text: '观察到模型容量不足', code: 'capacity', capacity: 1, success: 0 }, overview: { passed: 10, issues: 2, unknown: 1, advice: ['固定建议'] },
    doctor: { checks: [{ id: 'network.websocket_reachability', title: 'WebSocket', status: 'warning', reason: '<img src=x onerror=alert(1)>', duration_ms: 15000 }] },
    samples: [{ number: 1, status: 'failed', capacity_detected: true, reason: '所选模型容量不足 [capacity]', duration_seconds: 3 }] };
  const b = { ...structuredClone(a), id: 'b'.repeat(32), environment: 'home', started_at: a.started_at + 100,
    verdict: { text: '本轮模型请求通过', code: 'passed', success: 1, capacity: 0 }, overview: { passed: 13, issues: 0, unknown: 0, advice: [] },
    samples: [{ number: 1, status: 'success', capacity_detected: false, duration_seconds: 1, metrics: { ttft_seconds: .2 } }] };
  b.doctor.checks[0] = { id: 'network.websocket_reachability', title: 'WebSocket', status: 'ok', duration_ms: 50 };
  const state = { current: null, runs: [b, a], ready: true, failStart: false, deferRun: null };
  w.setTimeout = fn => { timers.push(fn); return timers.length; }; w.clearTimeout = () => {};
  w.fetch = async (url, options = {}) => {
    calls.push([url, options]);
    let value;
    if (url.endsWith('/start')) {
      if (state.failStart) { throw new Error('connection lost'); }
      const input = JSON.parse(options.body);
      state.current = { ...structuredClone(b), ...input, id: input.request_id, status: 'running', phase: '检查本机 CLI 网络', samples: [], overview: { passed: 0, issues: 0, unknown: 13 } };
      state.runs.unshift(state.current); value = state.current;
    } else if (url.endsWith('/cancel')) {
      state.current.status = 'stopping'; value = state.current;
    } else if (url.endsWith('/models')) { value = { ready: state.ready, models: state.ready ? ['gpt-test'] : [], cli_version: '1.0', auth_source: 'site' }; }
    else if (url.endsWith('/history')) { value = { runs: state.runs, total: state.runs.length }; }
    else if (url.includes('/run/')) {
      value = state.runs.find(row => url.endsWith(row.id));
      if (state.deferRun) { await state.deferRun(value); }
    }
    else { value = { current: state.current, busy: state.current?.status === 'running' }; }
    return { ok: true, json: async () => structuredClone(value) };
  };
  w.eval(fs.readFileSync(path.join(root, 'static/workspace.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(root, 'static/health.js'), 'utf8'));
  w.document.dispatchEvent(new w.CustomEvent('view-change', { detail: 'health' }));
  return { dom, w, calls, timers, state, a, b, get: name => w.document.getElementById('health-' + name) };
}

test('view and refresh only read; saved reports do not inject HTML', async () => {
  const p = page(); await settle();
  p.get('report').value = p.a.id; p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
  assert.match(p.get('verdict').textContent, /观察到模型容量不足/);
  assert.equal(p.get('result').querySelector('img'), null);
  assert.equal(p.get('success-count').textContent, '0 / 1');
  assert.equal(p.get('success-count').dataset.state, 'failed');
  p.get('refresh').click(); await settle();
  assert.equal(p.get('report').value, p.a.id);
  assert.ok(p.calls.every(([, options]) => !options.method));
  p.dom.window.close();
});

test('one click starts a complete check and double submission creates only one', async () => {
  const p = page(); await settle();
  p.get('start').click(); p.get('start').click();
  assert.match(p.get('progress').textContent, /正在提交/);
  assert.equal(p.get('start').disabled, true);
  await settle();
  const posts = p.calls.filter(([url]) => url.endsWith('/start'));
  assert.equal(posts.length, 1);
  assert.equal(JSON.parse(posts[0][1].body).model_check, true);
  assert.equal(JSON.parse(posts[0][1].body).diagnostics, false);
  assert.equal(JSON.parse(posts[0][1].body).svg_check, true);
  assert.equal(p.get('stop').disabled, false);
  assert.equal(p.get('report').value, p.state.current.id);
  p.get('stop').click(); await settle();
  assert.equal(p.state.current.status, 'stopping');
  assert.equal(JSON.parse(p.calls.at(-1)[1].body).run_id, p.state.current.id);
  p.dom.window.close();
});

test('uncertain submission retries reuse the same request ID', async () => {
  const p = page(); await settle(); p.state.failStart = true;
  p.get('start').click(); await settle();
  assert.equal(p.get('error').hidden, false);
  p.get('start').click(); await settle();
  const posts = p.calls.filter(([url]) => url.endsWith('/start')).map(([, options]) => JSON.parse(options.body));
  assert.equal(posts.length, 2);
  assert.equal(posts[0].request_id, posts[1].request_id);
  p.dom.window.close();
});

test('network-only is explicit and unavailable login does not prevent local diagnostics', async () => {
  const p = page(); await settle();
  p.state.ready = false; p.get('refresh').click(); await settle();
  assert.equal(p.get('start').disabled, false);
  p.get('model-check').checked = false; p.get('model-check').dispatchEvent(new p.w.Event('change'));
  assert.match(p.get('cost').textContent, /不发送模型输入/);
  p.get('start').click(); await settle();
  const body = JSON.parse(p.calls.find(([url]) => url.endsWith('/start'))[1].body);
  assert.equal(body.model_check, false); assert.equal(body.model, '');
  assert.equal(body.diagnostics, true);
  assert.equal(body.svg_check, false);
  p.dom.window.close();
});

/** Images stay inert, persist across polling and do not contaminate text speed medians. */
test('SVG preview is an image and polling keeps its node and separate measurements', async () => {
  const p = page(); await settle();
  Object.assign(p.b, { svg_check: true, sample_mode: 'performance',
    svg_result: { status: 'success', svg: '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect width="20" height="20"/></svg>',
      animated: false, duration_seconds: 12, metrics: { ttft_seconds: 3, end_to_end_tokens_per_second: 40, output_tokens: 480 } } });
  p.b.verdict.success = 2;
  await p.timers.at(-1)(); await settle();
  const image = p.get('svg-image');
  assert.ok(image);
  assert.match(image.src, /^data:image\/svg\+xml/);
  assert.equal(p.get('result').querySelector('script'), null);
  assert.equal(p.get('result').querySelector('iframe'), null);
  assert.equal(p.get('success-count').textContent, '2 / 2');
  assert.equal(p.get('ttft').textContent, '0.20 s');
  assert.match(p.get('result').querySelector('.health-svg').textContent, /3.00 s/);
  p.b.doctor.checks[0].reason = 'changed';
  await p.timers.at(-1)(); await settle();
  assert.equal(p.get('svg-image'), image);
  image.dispatchEvent(new p.w.Event('error'));
  assert.equal(image.hidden, true);
  assert.match(p.get('svg-render-error').textContent, /无法显示/);
  assert.ok(p.calls.every(([, options]) => !options.method));
  p.dom.window.close();
});

/** The new default remains optional and makes extra quota visible before submission. */
test('SVG can be disabled and unfinished or legacy records never show a generated image', async () => {
  const p = page(); await settle();
  assert.match(p.get('cost').textContent, /1 次文字测速 \+ 1 次 SVG/);
  assert.match(p.get('result').querySelector('.health-svg').textContent, /未检测/);
  p.get('svg-check').checked = false;
  p.get('svg-check').dispatchEvent(new p.w.Event('change'));
  assert.doesNotMatch(p.get('cost').textContent, /SVG/);
  p.get('start').click(); await settle();
  assert.equal(JSON.parse(p.calls.find(([url]) => url.endsWith('/start'))[1].body).svg_check, false);
  Object.assign(p.state.current, { svg_check: true, svg_result: { status: 'running' }, phase: '正在生成 SVG' });
  p.get('refresh').click(); await settle();
  assert.equal(p.get('result-title').textContent, '正在生成 SVG');
  assert.equal(p.get('svg-image'), null);
  assert.match(p.get('progress').textContent, /0 \/ 2/);
  p.dom.window.close();
});

test('comparison remains selected through polling and shows evidence limits', async () => {
  const p = page(); await settle();
  p.get('left').value = p.a.id; p.get('right').value = p.b.id;
  p.get('right').dispatchEvent(new p.w.Event('change')); await settle();
  assert.match(p.get('comparison').textContent, /公司/); assert.match(p.get('comparison').textContent, /家里/);
  assert.match(p.get('compare-note').textContent, /出口 IP/);
  p.get('environment').value = 'home'; p.get('repeats').value = '3';
  p.get('left').focus();
  await p.timers.at(-1)(); await settle();
  assert.equal(p.get('left').value, p.a.id);
  assert.equal(p.get('environment').value, 'home'); assert.equal(p.get('repeats').value, '3');
  assert.equal(p.w.document.activeElement, p.get('left'));
  assert.ok(p.calls.every(([, options]) => !options.method));
  p.dom.window.close();
});

/** Model evidence leads the page; local diagnostic counts are not a usability score. */
test('request verdict and metrics stay separate from local warnings', async () => {
  const p = page(); await settle();
  assert.equal(p.get('result-title').textContent, '本轮模型可用');
  assert.equal(p.get('summary').dataset.tone, 'success');
  assert.equal(p.get('result').querySelector('[data-health-disclosure=network]').open, false);
  p.get('report').value = p.a.id; p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
  assert.equal(p.get('result-title').textContent, '已复现模型容量不足');
  assert.equal(p.get('success-count').textContent, '0 / 1');
  assert.equal(p.get('speed').textContent, '—');
  assert.equal(p.get('result').querySelector('[data-health-disclosure=diagnostics]').open, false);
  assert.match(p.get('next-step').textContent, /不能确定是公司网络导致/);
  assert.ok(p.calls.every(([, options]) => !options.method)); p.dom.window.close();
});

/** Polling unchanged and changed snapshots must not collapse a check or steal focus. */
test('report disclosure and focus survive polling and record switches', async () => {
  const p = page(); await settle();
  const group = p.get('result').querySelector('[data-health-disclosure=network]');
  group.open = true; group.querySelector('summary').focus();
  await p.timers.at(-1)(); await settle();
  assert.equal(p.get('result').querySelector('[data-health-disclosure=network]'), group);
  p.b.doctor.checks[0].reason = 'updated snapshot';
  await p.timers.at(-1)(); await settle();
  const updated = p.get('result').querySelector('[data-health-disclosure=network]');
  assert.equal(updated.open, true);
  assert.equal(p.w.document.activeElement, updated.querySelector('summary'));
  p.get('report').value = p.a.id; p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
  p.get('report').value = p.b.id; p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
  assert.equal(p.get('result').querySelector('[data-health-disclosure=network]').open, true);
  p.dom.window.close();
});

/** Keyboard tabs and all comparison actions only inspect saved data; drafts survive. */
test('comparison defaults, swap, differences and keyboard tabs stay read-only', async () => {
  const p = page(); await settle();
  assert.equal(p.get('left').value, p.a.id); assert.equal(p.get('right').value, p.b.id);
  p.get('environment').value = 'home'; p.get('repeats').value = '3';
  p.get('check-tab').focus();
  p.get('check-tab').dispatchEvent(new p.w.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })); await settle();
  assert.equal(p.get('compare-panel').hidden, false);
  assert.equal(p.w.document.activeElement, p.get('compare-tab'));
  assert.equal(p.get('compare-tab').getAttribute('aria-selected'), 'true');
  const total = p.get('comparison').querySelectorAll('tbody tr').length;
  p.get('differences').checked = true; p.get('differences').dispatchEvent(new p.w.Event('change')); await settle();
  assert.ok(p.get('comparison').querySelectorAll('tbody tr').length < total);
  assert.ok([...p.get('comparison').querySelectorAll('tbody tr')].every(row => row.dataset.different === 'true'));
  p.get('swap').click(); await settle();
  assert.equal(p.get('left').value, p.b.id); assert.equal(p.get('right').value, p.a.id);
  p.get('compare-tab').dispatchEvent(new p.w.KeyboardEvent('keydown', { key: 'Home', bubbles: true })); await settle();
  assert.equal(p.get('check-panel').hidden, false);
  assert.equal(p.get('environment').value, 'home'); assert.equal(p.get('repeats').value, '3');
  p.get('compare-current').click(); await settle();
  assert.equal(p.get('compare-panel').hidden, false); assert.equal(p.get('left').value, p.get('report').value);
  assert.ok(p.calls.every(([, options]) => !options.method)); p.dom.window.close();
});

/** A successful partial sample cannot turn a stopped or unfinished run into an all-clear. */
test('unfinished and untested runs never display an overall success', async () => {
  const p = page(); await settle();
  for (const status of ['running', 'stopping', 'cancelled', 'interrupted', 'failed']) {
    p.b.status = status;
    p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
    assert.notEqual(p.get('summary').dataset.tone, 'success');
    assert.notEqual(p.get('result-title').textContent, '本轮模型可用');
  }
  p.b.status = 'completed'; p.b.model_check = false; p.b.samples = [];
  p.b.verdict = { code: 'not_tested', text: '尚未进行模型实测，容量未知' };
  p.b.overview.unknown = 1;
  p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
  assert.equal(p.get('result-title').textContent, '尚未验证模型可用性');
  assert.match(p.get('next-step').textContent, /连接检查不能证明模型可用/);
  p.dom.window.close();
});

/** A slow saved-report response cannot overwrite a more recent user selection. */
test('late report responses cannot replace the selected record', async () => {
  const p = page(); await settle(); let release;
  p.state.deferRun = run => run.id === p.a.id ? new Promise(resolve => { release = resolve; }) : Promise.resolve();
  p.get('report').value = p.a.id; p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
  p.get('report').value = p.b.id; p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
  release(); await settle();
  assert.equal(p.get('result-title').textContent, '本轮模型可用');
  assert.equal(p.get('report').value, p.b.id); p.dom.window.close();
});

/** Empty selections must not leave an unrelated old verdict visible. */
test('clearing a record hides its verdict and reselecting restores it', async () => {
  const p = page(); await settle();
  p.get('report').value = ''; p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
  assert.equal(p.get('summary').hidden, true); assert.equal(p.get('compare-current').disabled, true);
  p.get('report').value = p.b.id; p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
  assert.equal(p.get('summary').hidden, false); assert.equal(p.get('result').hidden, false);
  assert.equal(p.get('result-title').textContent, '本轮模型可用'); p.dom.window.close();
});

/** Successful samples alone determine speed; legacy short checks remain visibly unmeasured. */
test('speed summary uses valid-sample medians and retains legacy unknowns', async () => {
  const p = page(); await settle();
  assert.equal(p.get('ttft').textContent, '0.20 s');
  assert.equal(p.get('speed').textContent, '—');
  assert.equal(p.get('speed-note').textContent, '旧记录未测速');
  p.b.sample_mode = 'performance'; p.b.diagnostics = false; p.b.doctor = null; p.b.repeats = 3;
  p.b.samples = [
    { status: 'success', duration_seconds: 10, metrics: { ttft_seconds: 1, end_to_end_tokens_per_second: 20 } },
    { status: 'success', duration_seconds: 20, metrics: { ttft_seconds: 3, end_to_end_tokens_per_second: 40 } },
    { status: 'failed', duration_seconds: 200, metrics: { ttft_seconds: 100, end_to_end_tokens_per_second: 1000 } }
  ];
  p.get('report').dispatchEvent(new p.w.Event('change')); await settle();
  assert.equal(p.get('success-count').textContent, '2 / 3');
  assert.equal(p.get('ttft').textContent, '2.00 s');
  assert.equal(p.get('speed').textContent, '30.00 tokens/s');
  assert.equal(p.get('total').textContent, '15.00 s');
  assert.match(p.get('speed-note').textContent, /含等待与推理/);
  assert.equal(p.get('result').querySelector('[data-health-disclosure=diagnostics]'), null);
  assert.ok(p.calls.every(([, options]) => !options.method)); p.dom.window.close();
});
