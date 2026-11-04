const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');

/** Drain promises without running the page's repeating poll timer. */
async function settle() { for (let i = 0; i < 12; i++) { await new Promise(resolve => setImmediate(resolve)); } }

/** Run real page markup and scripts against a controlled snapshot with no model calls. */
function page() {
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'static/index.html'), 'utf8'), { url: 'http://127.0.0.1:8780/#availability', runScripts: 'outside-only' });
  const window = dom.window, calls = [], timers = [];
  const site = '<img src=x onerror=alert(1)>';
  const now = Date.now() / 1000;
  const state = { settings: { enabled: false, sites: [{ site, enabled: true, model: 'chat' }] }, busy: false, active_count: 0,
    start: now - 86400, now, next_at: null, rows: [{ site, enabled: true, model: 'chat', state: 'unknown', latest: null,
      availability: null, total: 0, success: 0, failure: 0, unknown: 0,
      buckets: Array.from({ length: 60 }, (_, i) => ({ start: now - (60 - i) * 1440, end: now - (59 - i) * 1440, success: 0, failure: 0, unknown: 0, running: 0 })) }] };
  window.setTimeout = fn => { timers.push(fn); return timers.length; };
  window.clearTimeout = () => {};
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new window.Event('close')); };
  window.fetch = async (url, options = {}) => {
    calls.push([url, options]);
    if (url.includes('/settings')) { state.settings = JSON.parse(options.body); }
    if (url.includes('/check')) { state.busy = true; state.active_count = 1; }
    const value = url.includes('/history?') ? { rows: [], page: 1, total: 0, page_size: 50 } : structuredClone(state);
    return { ok: true, json: async () => value };
  };
  window.eval(fs.readFileSync(path.join(root, 'static/availability.js'), 'utf8'));
  window.document.dispatchEvent(new window.CustomEvent('view-change', { detail: 'availability' }));
  return { dom, window, calls, timers, state, get: id => window.document.getElementById('availability-' + id) };
}

test('empty history stays gray and polling does not issue model probes', async () => {
  const p = page(); await settle();
  assert.match(p.get('list').textContent, /暂无记录/);
  assert.equal(p.get('list').querySelectorAll('[data-state=success]').length, 0);
  assert.equal(p.get('list').querySelector('img'), null);
  await p.timers.pop()(); await settle();
  assert.ok(p.calls.every(([, options]) => !options.method));
  p.dom.window.close();
});

test('polling preserves an open historical range, focused point and unsaved model', async () => {
  const p = page(); await settle();
  const input = p.get('site-settings').querySelector('input:not([type=checkbox])');
  input.value = 'new-model'; input.dispatchEvent(new p.window.Event('input', { bubbles: true }));
  const point = p.get('list').querySelector('.availability-bars button');
  point.focus(); point.click(); await settle();
  const range = p.get('detail-range').textContent;
  await p.timers.pop()(); await settle();
  assert.equal(p.get('detail').open, true);
  assert.equal(p.get('detail-range').textContent, range);
  assert.equal(p.window.document.activeElement, point);
  assert.equal(input.value, 'new-model');
  p.dom.window.close();
});

test('refresh responds immediately and a double click only reads saved status once', async () => {
  const p = page(); await settle();
  const before = p.calls.length;
  p.get('check').click(); p.get('check').click();
  assert.equal(p.get('check').disabled, true);
  assert.match(p.get('progress').textContent, /正在刷新/);
  await settle();
  assert.equal(p.calls.length, before + 1);
  assert.ok(p.calls.every(([, options]) => !options.method));
  assert.equal(p.get('check').disabled, false);
  assert.equal(p.get('check').textContent, '刷新状态');
  p.dom.window.close();
});

test('range switches request the chosen window and mixed buckets stay visible', async () => {
  const p = page(); await settle();
  p.state.rows[0].buckets[59].failure = 1; p.state.rows[0].buckets[59].success = 2;
  p.window.document.querySelector('[data-availability-hours="168"]').click(); await settle();
  assert.equal(p.calls.at(-1)[0], '/api/availability?hours=168');
  assert.equal(p.get('list').querySelectorAll('.availability-bars [data-state=mixed]').length, 1);
  p.dom.window.close();
});

test('hover uses real cached details and preserves the chosen interval across polling', async () => {
  const p = page(); await settle();
  const bucket = p.state.rows[0].buckets[59];
  bucket.success = 1;
  bucket.latest = { started_at: p.state.now - 10, status: 'success', duration_seconds: 1.885, reason: '<img src=x onerror=alert(1)>' };
  await p.timers.pop()(); await settle();
  const point = p.get('list').querySelector('.availability-bars button:last-child');
  const requests = p.calls.length;
  point.dispatchEvent(new p.window.Event('pointerenter'));
  assert.equal(p.get('tooltip').hidden, false);
  assert.match(p.get('tooltip').textContent, /1,885 ms/);
  assert.equal(p.get('tooltip').querySelector('img'), null);
  assert.equal(p.calls.length, requests);
  const preview = p.get('tooltip').textContent;
  const originalEnd = bucket.end;
  bucket.end += 15; bucket.latest.duration_seconds = 5;
  await p.timers.pop()(); await settle();
  assert.equal(p.get('tooltip').textContent, preview);
  assert.equal(p.get('list').querySelector('.availability-bars button:last-child'), point);
  point.click(); await settle();
  assert.equal(p.get('tooltip').hidden, true);
  assert.equal(Number(new URL(p.calls.at(-1)[0], 'http://localhost').searchParams.get('end')), originalEnd);
  p.dom.window.close();
});

test('keyboard previews show missing samples honestly and Escape dismisses them', async () => {
  const p = page(); await settle();
  const point = p.get('list').querySelector('.availability-bars button');
  point.focus();
  assert.equal(p.get('tooltip').hidden, false);
  assert.match(p.get('tooltip').textContent, /未检测/);
  assert.match(p.get('tooltip').textContent, /没有检测记录/);
  assert.doesNotMatch(p.get('tooltip').textContent, /0 ms/);
  p.window.document.dispatchEvent(new p.window.KeyboardEvent('keydown', { key: 'Escape' }));
  assert.equal(p.get('tooltip').hidden, true);
  assert.equal(point.hasAttribute('aria-describedby'), false);
  assert.match(p.get('list').querySelector('.availability-countdown').textContent, /已暂停/);
  p.dom.window.close();
});

test('preview labels the last attempt independently of earlier successes in the interval', async () => {
  const p = page(); await settle();
  const bucket = p.state.rows[0].buckets[59];
  bucket.success = 1; bucket.unknown = 1;
  bucket.latest = { started_at: p.state.now - 10, status: 'unknown', duration_seconds: null, reason: '服务重启，检测结果未知' };
  await p.timers.pop()(); await settle();
  p.get('list').querySelector('.availability-bars button:last-child').focus();
  assert.equal(p.get('tooltip').querySelector('.availability-state').textContent, '未知');
  assert.match(p.get('tooltip').textContent, /1 成功 \/ 0 失败 \/ 1 未知/);
  assert.match(p.get('tooltip').textContent, /服务重启/);
  p.dom.window.close();
});

test('daily token subtotal excludes estimates and shows missing coverage without moving cards', async () => {
  const p = page(); await settle();
  const card = p.get('list').querySelector('.availability-site');
  assert.equal(card.querySelector('.availability-tokens').textContent, '—');
  p.state.rows[0].today_usage = { start: p.state.now - 60, requests: 5, reported_requests: 2,
    estimated_requests: 1, missing_requests: 1, running_requests: 1, total_tokens: 1234, estimated_tokens: 50 };
  await p.timers.pop()(); await settle();
  assert.equal(p.get('list').querySelector('.availability-site'), card);
  assert.equal(card.querySelector('.availability-tokens').textContent, '1,234');
  assert.match(card.querySelector('.availability-usage-note').textContent, /已统计 2 \/ 5 次 · 1 次仅有估算 · 1 次用量未知 · 1 次检测中/);
  assert.match(card.querySelector('.availability-usage').title, /估算 50 token，未计入/);
  p.window.document.querySelector('[data-availability-hours="720"]').click(); await settle();
  assert.equal(card.querySelector('.availability-tokens').textContent, '1,234');
  assert.ok(p.calls.every(([, options]) => !options.method));
  p.state.rows[0].today_usage.total_tokens = 0;
  await p.timers.pop()(); await settle();
  assert.equal(card.querySelector('.availability-tokens').textContent, '0');
  p.dom.window.close();
});

test('failed probe preview includes usage and identifies reasoning as a subset', async () => {
  const p = page(); await settle();
  p.state.rows[0].buckets[59].latest = { started_at: p.state.now, status: 'failure', reason: '回答被截断',
    usage: { source: 'reported', total_tokens: 130, prompt_tokens: 100, completion_tokens: 30, reasoning_tokens: 28, cached_tokens: 90 } };
  await p.timers.pop()(); await settle();
  p.get('list').querySelector('.availability-bars button:last-child').focus();
  assert.match(p.get('tooltip').textContent, /接口返回 130 token · 输入 100 · 输出 30 · 其中推理 28 · 输入中缓存 90/);
  assert.equal(p.get('tooltip').querySelector('.availability-state').textContent, '异常');
  p.dom.window.close();
});
