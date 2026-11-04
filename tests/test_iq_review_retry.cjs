/** 使用真实页面、合成记录验证整批复核交互，所有模型请求均为模拟。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

/**
 * 装载页面并提供可控的提交回包、运行状态。
 * @param {object} t node:test 上下文，用于关闭窗口与定时器
 * @return {Promise<object>} DOM、请求列表及合成批次
 */
async function fixture(t) {
  const root = path.join(__dirname, '..');
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'static/index.html'), 'utf8'), {
    runScripts: 'outside-only', url: 'http://127.0.0.1:8780/#iq'
  });
  t.after(() => dom.window.close());
  dom.window.HTMLElement.prototype.scrollIntoView = function () {};
  const suite = { id: 'bbeh-mini', benchmark: 'bbeh-mini', formal: true, count: 2, version: 'fixture' };
  const run = { run_id: 'a'.repeat(32), status: 'completed', done: 2, total: 2, suite,
    summary: [], options: { repeats: 1, concurrency: 32, timeout_seconds: 600 },
    auto_review: { status: 'partial', total: 2, done: 2, failed: 2 } };
  const row = { site: 'site', model: 'model', valid: 2, planned: 2, categories: {},
    final: { pending: 2, review_failed: 2, reviewed: 0 }, normalized: {} };
  const calls = [];
  const f = { run, calls, row, dom, post: async body => {
    run.auto_review = { id: 'b'.repeat(32), run_id: run.run_id, trigger: 'retry_failed',
      retry_request: body, judge_site: body.judge_site, judge_model: body.judge_model,
      status: 'running', done: 0, total: 2, completed: 0, failed: 0 };
    return run.auto_review;
  } };
  dom.window.fetch = async (url, options) => {
    const body = options?.body ? JSON.parse(options.body) : null;
    calls.push({ url, body });
    let data;
    if (url === '/api/config') { data = { sites: [{ name: 'judge', models: ['judge-model'] }],
      iq_auto_review_site: 'judge', iq_auto_review_model: 'judge-model' }; }
    else if (url === '/api/gallery') { data = { cells: [] }; }
    else if (url === '/api/iq/suites') { data = { suites: [suite] }; }
    else if (url === '/api/iq/runs') { data = { runs: [{ ...run, summary: [row] }] }; }
    else if (url.startsWith('/api/iq/leaderboard')) { data = { scoring: 'comparison', entries: [], cohorts: [] }; }
    else if (url.startsWith('/api/iq/run/')) { data = { ...run, run_id: url.split('/').pop(), summary: [row] }; }
    else if (url === '/api/iq/review-retry') { data = await f.post(body); }
    else { throw new Error('Unexpected API: ' + url); }
    return { ok: true, json: async () => structuredClone(data) };
  };
  dom.window.eval(fs.readFileSync(path.join(root, 'static/iq.js'), 'utf8'));
  f.$ = selector => dom.window.document.querySelector(selector);
  f.tick = () => new Promise(resolve => setImmediate(resolve));
  f.posts = () => calls.filter(c => c.body);
  f.refresh = async () => { f.$('#iq-refresh').click(); await f.tick(); };
  dom.window.document.dispatchEvent(new dom.window.CustomEvent('view-change', { detail: 'iq' }));
  await f.tick();
  assert.equal(f.$('#iq-error').hidden, true, f.$('#iq-error').textContent);
  return f;
}

test('batch retry is visible, confirms count and reviewer, cancel sends nothing', async t => {
  const f = await fixture(t);
  assert.equal(f.$('#iq-review-retry').hidden, false);
  assert.match(f.$('#iq-review-retry').textContent, /重试失败复核（2）/);
  f.$('#iq-review-retry').click();
  assert.equal(f.$('#iq-review-retry-confirm').hidden, false);
  assert.match(f.$('#iq-review-retry-confirm-description').textContent, /2 条.*judge \/ judge-model.*不重新答题.*消耗/);
  f.$('#iq-review-retry-cancel').click();
  assert.equal(f.$('#iq-review-retry-confirm').hidden, true);
  assert.equal(f.posts().length, 0);
});

test('submit gives immediate feedback, prevents double submit and polls progress', async t => {
  const f = await fixture(t);
  const accept = f.post;
  let resolve;
  f.post = body => new Promise(done => { resolve = async () => done(await accept(body)); });
  f.$('#iq-review-retry').click();
  f.$('#iq-review-retry-submit').click();
  f.$('#iq-review-retry-submit').click();
  assert.match(f.$('#iq-review-retry-feedback').textContent, /正在提交/);
  assert.equal(f.posts().length, 1);
  assert.equal(f.$('#iq-review-retry').disabled, true);
  await resolve(); await f.tick();
  assert.match(f.$('#iq-review-retry-feedback').textContent, /复核重试中.*0 \/ 2/);
  assert.equal(f.$('#iq-progress').getAttribute('aria-label'), '复核重试进度');
  f.run.auto_review.done = 1;
  await new Promise(done => setTimeout(done, 1900));
  assert.match(f.$('#iq-review-retry-feedback').textContent, /1 \/ 2/);
  Object.assign(f.run.auto_review, { status: 'completed', done: 2, completed: 2 });
  Object.assign(f.row.final, { pending: 0, review_failed: 0, reviewed: 2 });
  await f.refresh();
  assert.match(f.$('#iq-review-retry-feedback').textContent, /复核重试完成.*2 \/ 2.*完成 2 条，失败 0 条/);
  assert.equal(f.$('#iq-review-retry').hidden, true);
  assert.equal(f.posts().length, 1);
});

test('unknown submission reuses the same request instead of starting another round', async t => {
  const f = await fixture(t);
  const accept = f.post;
  f.post = async () => { throw new Error('connection lost'); };
  f.$('#iq-review-retry').click(); f.$('#iq-review-retry-submit').click(); await f.tick();
  assert.match(f.$('#iq-review-retry-feedback').textContent, /未能确认.*connection lost.*同一请求标识/);
  f.post = accept;
  f.$('#iq-review-retry').click(); f.$('#iq-review-retry-submit').click(); await f.tick();
  assert.equal(f.posts().length, 2);
  assert.deepEqual(f.posts()[0].body, f.posts()[1].body);
  assert.match(f.$('#iq-review-retry-feedback').textContent, /复核重试中/);
});

test('lost POST response discovers the accepted request through GET', async t => {
  const f = await fixture(t);
  const accept = f.post;
  f.post = async body => { await accept(body); throw new Error('lost response'); };
  f.$('#iq-review-retry').click(); f.$('#iq-review-retry-submit').click(); await f.tick();
  assert.match(f.$('#iq-review-retry-feedback').textContent, /复核重试中/);
  assert.doesNotMatch(f.$('#iq-review-retry-feedback').textContent, /未能确认/);
  assert.equal(f.$('#iq-review-retry').disabled, true);
  assert.equal(f.posts().length, 1);
});

test('remaining failures allow a new explicit round with a new id', async t => {
  const f = await fixture(t);
  f.$('#iq-review-retry').click(); f.$('#iq-review-retry-submit').click(); await f.tick();
  Object.assign(f.run.auto_review, { status: 'partial', done: 2, completed: 1, failed: 1 });
  Object.assign(f.row.final, { pending: 1, review_failed: 1, reviewed: 1 });
  await f.refresh();
  assert.match(f.$('#iq-review-retry').textContent, /（1）/);
  f.$('#iq-review-retry').click(); f.$('#iq-review-retry-submit').click(); await f.tick();
  assert.equal(f.posts().length, 2);
  assert.notEqual(f.posts()[0].body.request_id, f.posts()[1].body.request_id);
  assert.equal(f.posts()[0].body.run_id, f.posts()[1].body.run_id);
});

test('switching history while submitting cannot overwrite the selected run', async t => {
  const f = await fixture(t);
  let finish;
  f.post = body => new Promise(resolve => { finish = () => resolve({ trigger: 'retry_failed',
    status: 'running', total: 2, done: 0, run_id: body.run_id, retry_request: body }); });
  f.$('#iq-review-retry').click(); f.$('#iq-review-retry-submit').click();
  const id = 'c'.repeat(32);
  const select = f.$('#iq-history');
  select.add(new f.dom.window.Option('another', id)); select.value = id;
  select.dispatchEvent(new f.dom.window.Event('change'));
  await f.tick();
  finish(); await f.tick();
  assert.equal(f.$('#iq-review-retry-feedback').hidden, true);
  assert.equal(f.$('#iq-review-retry').disabled, false);
});
