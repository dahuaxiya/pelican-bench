/** 双列榜单使用真实页面和合成 API，不发起模型请求或写入历史。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

/**
 * 创建同批成绩，null 最终分表示待复核。
 * @param {string} model 模型名称，也用于构造可断言的行标识
 * @param {number} before 原始分数
 * @param {number|null} after 最终分数
 * @param {number} seconds 平均耗时
 * @return {object} 不包含凭据的 API 摘要
 */
function entry(model, before, after, seconds = 10) {
  return { model, site: 'Site A', complete: true, mean_seconds: seconds,
    before_review: { rank_score: before }, after_review: { rank_score: after, ready: after !== null },
    run_id: 'a'.repeat(32), completed_at: 1800000000 };
}

/**
 * 初始化正式脚本，替换网络读取以验证排序与分页，不复制业务算法。
 * @param {object} t 测试上下文，负责关闭计时器和 DOM
 * @param {Array<object>} entries 合成榜单行
 * @return {Promise<object>} 页面查询、选择器和可变榜单快照
 */
async function fixture(t, entries) {
  const root = path.join(__dirname, '..');
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'static/index.html'), 'utf8'), {
    runScripts: 'outside-only', url: 'http://127.0.0.1:8780/#iq'
  });
  t.after(() => dom.window.close());
  dom.window.HTMLElement.prototype.scrollIntoView = function () {};
  const suite = { id: 'bbeh-mini', benchmark: 'bbeh-mini', formal: true, count: 460, version: 'fixture' };
  const board = { scoring: 'comparison', cohort_id: 'fixture', cohorts: [{ id: 'fixture', suite }], entries };
  const calls = [];
  dom.window.fetch = async (url, options) => {
    calls.push({ url, method: options?.method || 'GET' });
    let data;
    if (url === '/api/config') { data = { sites: [] }; }
    else if (url === '/api/gallery') { data = { cells: [] }; }
    else if (url === '/api/iq/suites') { data = { suites: [suite] }; }
    else if (url === '/api/iq/runs') { data = { runs: [] }; }
    else if (url.startsWith('/api/iq/leaderboard')) { data = board; }
    else if (url.startsWith('/api/iq/reviews')) { data = { reviews: [] }; }
    else if (url.startsWith('/api/iq/run/')) {
      data = { run_id: 'a'.repeat(32), status: 'completed', done: 0, total: 0, suite, summary: [],
        questions: [], results: [], options: { repeats: 1, concurrency: 32, timeout_seconds: 600 } };
    } else { throw new Error('Unexpected API: ' + url); }
    return { ok: true, json: async () => structuredClone(data) };
  };
  dom.window.eval(fs.readFileSync(path.join(root, 'static/iq.js'), 'utf8'));
  const $ = selector => dom.window.document.querySelector(selector);
  const all = selector => Array.from(dom.window.document.querySelectorAll(selector));
  const tick = () => new Promise(resolve => setImmediate(resolve));
  const choose = (selector, value) => {
    $(selector).value = value; $(selector).dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  };
  dom.window.document.dispatchEvent(new dom.window.CustomEvent('view-change', { detail: 'iq' }));
  await tick();
  assert.equal($('#iq-rank-error').hidden, true, $('#iq-rank-error').textContent);
  return { dom, $, all, choose, calls, board, tick };
}

test('two scores and ranks stay visible while sort basis changes locally', async t => {
  const f = await fixture(t, [entry('A', 80, 81, 30), entry('B', 70, 90, 20), entry('C', 60, null, 10)]);
  const models = () => f.all('.iq-bar-identity strong').map(el => el.textContent);
  assert.deepEqual(models(), ['B', 'A', 'C']);
  const before = f.$('.iq-score-before'); const after = f.$('.iq-score-after');
  assert.match(before.textContent, /复核前#270.00%/);
  assert.match(after.textContent, /复核后#190.00%/);
  assert.equal(f.$('#iq-rank-scoring'), null);
  assert.equal(f.$('#iq-rank-sort option[value=normalized]'), null);
  assert.equal(f.$('#iq-rank-version-wrap').hidden, true);
  const count = f.calls.length;
  f.choose('#iq-rank-sort', 'before');
  assert.deepEqual(models(), ['A', 'B', 'C']);
  f.choose('#iq-rank-sort', 'mean_seconds');
  assert.deepEqual(models(), ['C', 'B', 'A']);
  assert.equal(f.$('#iq-metric-score').textContent, '90.00%');
  assert.equal(f.calls.length, count);
  assert.ok(f.calls.every(call => call.method === 'GET'));
});

test('pending final stays unranked and zero is valid in both views', async t => {
  const f = await fixture(t, [entry('pending', 95, null), entry('zero', 0, 0)]);
  assert.deepEqual(f.all('.iq-bar-rank').map(el => el.textContent), ['1', '—']);
  assert.equal(f.$('.iq-score-after [role=meter]').getAttribute('aria-valuenow'), '0');
  assert.equal(f.all('.iq-score-after')[1].querySelector('[role=meter]'), null);
  f.$('#iq-view-table').click();
  const rows = f.all('#iq-rank-table tbody tr');
  assert.match(rows[0].children[4].textContent, /#10.00%/);
  assert.match(rows[1].children[4].textContent, /未排名待复核/);
  assert.equal(rows[1].children[0].textContent, '—');
  assert.match(rows[1].children[3].textContent, /#195.00%/);
});

test('ties survive pagination and site filtering recalculates both ranks', async t => {
  const rows = Array.from({ length: 22 }, (_, index) => entry('model-' + index,
    index === 10 ? 91 : 100 - index, index === 10 ? 91 : 100 - index));
  rows[10].site = 'Site B';
  const f = await fixture(t, rows);
  f.$('#iq-rank-next').click();
  assert.equal(f.$('#iq-rank-page').value, '2');
  assert.equal(f.$('.iq-bar-rank').textContent, '10');
  assert.match(f.$('.iq-score-before').textContent, /#1091.00%/);
  f.$('#iq-view-table').click();
  assert.equal(f.$('#iq-rank-table tbody tr').children[0].textContent, '10');
  f.$('input[name=iq-rank-mode][value=site]').click();
  f.choose('#iq-rank-scope', 'Site B');
  assert.equal(f.$('#iq-rank-page').value, '1');
  assert.match(f.$('#iq-rank-table tbody tr').children[4].textContent, /#191.00%/);
});

test('multiple editions remain selectable and model names are text only', async t => {
  const f = await fixture(t, [entry('<img src=x onerror=alert(1)>', 50, 70)]);
  assert.equal(f.$('#iq-rank-table img'), null);
  assert.match(f.$('.iq-bar-identity').textContent, /<img/);
  f.board.cohorts.push({ id: 'another', suite: { version: 'v2', count: 460 } });
  f.$('#iq-rank-refresh').click(); await f.tick();
  assert.equal(f.$('#iq-rank-version-wrap').hidden, false);
  assert.equal(f.$('#iq-rank-cohort').options.length, 2);
});

test('legacy single-score response is not mislabeled as comparison', async t => {
  const f = await fixture(t, [entry('A', 50, 70)]);
  f.board.scoring = 'official';
  f.$('#iq-rank-refresh').click(); await f.tick();
  assert.equal(f.$('#iq-rank-error').hidden, false);
  assert.match(f.$('#iq-rank-error').textContent, /更新后端/);
  assert.equal(f.$('#iq-rank-table').children.length, 0);
});
