const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');

/** Flush chained fetch continuations without starting real polling timers. */
async function settle() { for (let i = 0; i < 12; i++) { await new Promise(resolve => setImmediate(resolve)); } }

/** Isolate the node page with controllable backend snapshots and no model traffic. */
function page(initial = { status: 'idle' }) {
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'static/index.html'), 'utf8'), { url: 'http://127.0.0.1:8780/#network', runScripts: 'outside-only' });
  const window = dom.window, calls = [], timers = [];
  const state = { value: initial, error: null };
  window.setTimeout = fn => { timers.push(fn); return timers.length; };
  window.clearTimeout = () => {};
  window.fetch = async (url, options = {}) => {
    calls.push([url, options]);
    if (url.endsWith('/apply') && options.method === 'POST') {
      if (state.applyError) { return { ok: false, json: async () => ({ error: state.applyError }) }; }
    }
    if (state.error && options.method === 'POST' && url.endsWith('/run')) { return { ok: false, json: async () => ({ error: state.error }) }; }
    let value = state.value;
    if (url.endsWith('/config')) { value = { controller: 'http://127.0.0.1:9090', proxy: 'http://127.0.0.1:7890', group: 'group', secret_set: true }; }
    if (url.endsWith('/models')) { value = { ready: true, models: ['gpt-6.1-sol', 'gpt-test'] }; }
    if (url.includes('/history')) { value = { items: [], page: 1, total: 0 }; }
    if (url.endsWith('/run') && options.method === 'POST') { value = state.value = { id: 'a'.repeat(12), status: 'running', phase: 'https', screened: 0, screen_total: 3 }; }
    if (url.endsWith('/apply') && options.method === 'POST') {
      value = state.value = { ...state.value, application: { status: 'running', phase: 'verifying', target: 'best' } };
    }
    return { ok: true, json: async () => value };
  };
  window.eval(fs.readFileSync(path.join(root, 'static/network.js'), 'utf8'));
  window.document.dispatchEvent(new window.CustomEvent('view-change', { detail: 'network' }));
  return { dom, window, calls, state, timers, get: id => window.document.getElementById('network-' + id) };
}

test('entering and polling never initiate a node switch or reset a form draft', async () => {
  const p = page(); await settle();
  p.get('top').value = '11'; p.get('top').focus();
  await p.timers.pop()(); await settle();
  assert.equal(p.get('top').value, '11');
  assert.equal(p.window.document.activeElement, p.get('top'));
  assert.ok(p.calls.every(([, options]) => !options.method));
  p.dom.window.close();
});

test('submission gives immediate feedback and shares the active batch', async () => {
  const p = page(); await settle();
  p.get('form').dispatchEvent(new p.window.Event('submit', { cancelable: true }));
  assert.equal(p.get('start').disabled, true);
  assert.match(p.get('message').textContent, /提交/);
  await settle();
  assert.equal(p.calls.filter(([url, options]) => url.endsWith('/run') && options.method === 'POST').length, 1);
  assert.equal(JSON.parse(p.calls.find(([url, options]) => url.endsWith('/run') && options.method === 'POST')[1].body).concurrency, 6);
  assert.match(p.get('progress').textContent, /HTTPS 初筛 0\/3/);
  assert.equal(p.get('cancel').hidden, false);
  p.dom.window.close();
});

test('server refusal stays visible and re-enables retry', async () => {
  const p = page(); await settle(); p.state.error = '请先结束当前评测';
  p.get('form').dispatchEvent(new p.window.Event('submit', { cancelable: true })); await settle();
  assert.match(p.get('error').textContent, /当前评测/);
  assert.equal(p.get('start').disabled, false);
  assert.equal(p.get('error').hidden, false);
  p.dom.window.close();
});

test('sample details survive polling and node names are never interpreted as HTML', async () => {
  const p = page(); await settle();
  p.state.value = { id: 'a'.repeat(12), status: 'running', phase: 'websocket', done: 1, total: 5,
    rows: [{ name: '<img src=x onerror=alert(1)>', rank: null, success: 1, attempts: 1, complete: false,
      median_ms: 15, max_success_ms: 15, spread_ms: null, samples: [{ ok: true, elapsed_ms: 15 }] }] };
  await p.timers.pop()(); await settle();
  const details = p.get('rank').querySelector('details'); details.open = true; details.querySelector('summary').focus();
  p.state.value.done = 2;
  await p.timers.pop()(); await settle();
  assert.equal(p.get('rank').querySelector('details').open, true);
  assert.equal(p.window.document.activeElement, p.get('rank').querySelector('summary'));
  assert.equal(p.get('rank').querySelector('img'), null);
  assert.match(p.get('rank').textContent, /未满/);
  p.dom.window.close();
});

test('one click applies the visible batch and shows verification then success', async () => {
  const p = page(); await settle();
  assert.equal(p.get('apply').disabled, true);
  p.state.value = { id: 'b'.repeat(12), status: 'completed', recommendations: ['best'], rows: [] };
  await p.timers.pop()(); await settle();
  assert.equal(p.get('apply').disabled, false);
  p.get('apply').click(); p.get('apply').click();
  assert.equal(p.get('apply').disabled, true);
  assert.match(p.get('application').textContent, /正在切换/);
  await settle();
  const calls = p.calls.filter(([url, options]) => url.endsWith('/apply') && options.method === 'POST');
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0][1].body), { run_id: 'b'.repeat(12) });
  assert.match(p.get('apply').textContent, /验证中/);
  assert.equal(p.get('start').disabled, true);
  p.state.value.application = { status: 'succeeded', target: 'best', finished_at: 123, restoration: { state: 'retained', node: 'best' } };
  await p.timers.pop()(); await settle();
  assert.match(p.get('application').textContent, /已切换：best.*验证通过/);
  assert.equal(p.get('start').disabled, false);
  p.dom.window.close();
});

test('apply refusal and rollback remain visible after polling', async () => {
  const p = page(); await settle();
  p.state.value = { id: 'b'.repeat(12), status: 'completed', recommendations: ['best'] };
  await p.timers.pop()(); await settle();
  p.state.applyError = '请先结束当前评测';
  p.get('apply').click(); await settle();
  await p.timers.pop()(); await settle();
  assert.match(p.get('error').textContent, /当前评测/);
  assert.equal(p.get('apply').disabled, false);
  p.state.value.application = { status: 'failed', target: 'best', failure: '验证失败', restoration: { state: 'restored', node: 'previous' } };
  await p.timers.pop()(); await settle();
  assert.match(p.get('application').textContent, /验证失败.*已恢复点击前的节点：previous/);
  p.dom.window.close();
});

test('model testing is opt-in and sends the selected official model and metric', async () => {
  const p = page(); await settle();
  assert.equal(p.get('model-options').hidden, true);
  assert.equal(p.calls.some(([url]) => url.endsWith('/models')), false);
  p.get('mode-model').click(); await settle();
  assert.equal(p.get('model-options').hidden, false);
  assert.equal(p.get('model').value, 'gpt-6.1-sol');
  assert.equal(p.get('timeout').value, '180');
  p.get('sort').value = 'throughput';
  p.get('concurrency').value = '3';
  p.get('form').dispatchEvent(new p.window.Event('submit', { cancelable: true })); await settle();
  const call = p.calls.find(([url, options]) => url.endsWith('/run') && options.method === 'POST');
  assert.equal(JSON.parse(call[1].body).mode, 'model');
  assert.equal(JSON.parse(call[1].body).model, 'gpt-6.1-sol');
  assert.equal(JSON.parse(call[1].body).sort_by, 'throughput');
  assert.equal(JSON.parse(call[1].body).timeout, 180);
  assert.equal(JSON.parse(call[1].body).concurrency, 3);
  p.dom.window.close();
});

test('concurrent node progress is separate and restores CLI concurrency', async () => {
  const p = page({ id: 'd'.repeat(12), status: 'running', phase: 'model', done: 0, total: 4, running: 2, queued: 0,
    options: { mode: 'model', model: 'gpt-test', repeats: 2, top: 2, timeout: 180, concurrency: 2 },
    rows: ['first', 'second'].map((name, index) => ({ name, samples: [], state: 'running', round: 1,
      success: 0, attempts: 0, metric_samples: 0, output_observation: index ? null : { elapsed_seconds: 2 } })) });
  await settle();
  assert.equal(p.get('concurrency').value, '2');
  assert.match(p.get('progress').textContent, /并发 2 · 运行 2 · 排队 0/);
  const rows = p.get('rank').querySelectorAll('tbody tr');
  assert.match(rows[0].textContent, /生成中.*首字 2000 ms/);
  assert.match(rows[1].textContent, /等待首字/);
  p.dom.window.close();
});

test('model rankings display real metrics and preserve unknown fields', async () => {
  const p = page(); await settle();
  p.state.value = { id: 'a'.repeat(12), status: 'running', phase: 'model', done: 1, total: 2,
    current_node: 'node', round: 1, output_observation: { elapsed_seconds: 1.25 },
    options: { mode: 'model', model: 'gpt-test', sort_by: 'ttft' },
    rows: [{ name: 'node', rank: null, success: 1, attempts: 1, complete: false, metric_samples: 0,
      median_ms: 12000, median_ttft_ms: null, median_tokens_per_second: null,
      samples: [{ ok: true, elapsed_ms: 12000, metrics: { ttft_seconds: 1.25, output_tokens: null } }] }] };
  await p.timers.pop()(); await settle();
  assert.match(p.get('result-title').textContent, /模型实测/);
  assert.match(p.get('progress').textContent, /已出首字 1250 ms/);
  assert.match(p.get('rank').textContent, /缺少首字或有效用量，不排名/);
  assert.match(p.get('rank').textContent, /输出 未知 token/);
  assert.equal(p.get('rank').querySelectorAll('tbody td')[3].textContent, '—');
  p.dom.window.close();
});

test('opening an active CLI model test restores its plan and updates waiting time', async () => {
  const started = Math.floor(Date.now() / 1000) - 10;
  const p = page({ id: 'c'.repeat(12), status: 'running', phase: 'model', done: 0, total: 2,
    current_node: 'node', current_started_at: started, round: 1, rows: [],
    options: { mode: 'model', model: 'gpt-test', repeats: 1, top: 2, timeout: 180, sort_by: 'throughput' } });
  await settle();
  assert.equal(p.get('mode-model').checked, true);
  assert.equal(p.get('model').value, 'gpt-test');
  assert.equal(p.get('timeout').value, '180');
  assert.equal(p.get('top').value, '2');
  p.window.Date.now = () => (started + 25) * 1000;
  await p.timers.pop()(); await settle();
  assert.match(p.get('progress').textContent, /等待首字 25s/);
  p.dom.window.close();
});
