/** 验证正式控件能在生成期间展示首字，且旧记录不会被补造数值。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

/** 挂载真实脚本，以只读替身接口返回指定运行快照。
 * @param {object} t 测试上下文，用于回收轮询定时器。
 * @param {object} row 受控的模型行，不含真实账号数据。
 * @return {Promise<HTMLElement>} 已完成首轮渲染的容器。
 */
async function renderRow(t, row) {
  const dom = new JSDOM('<div id="host"></div>', { runScripts: 'outside-only', url: 'http://127.0.0.1:8780' });
  t.after(() => dom.window.close());
  dom.window.fetch = async () => ({ ok: true, json: async () => ({
    status: row.status === 'running' ? 'running' : 'completed', mode: 'performance', rows: [row]
  }) });
  dom.window.eval(fs.readFileSync(path.join(__dirname, '../static/capacity.js'), 'utf8'));
  const host = dom.window.document.querySelector('#host');
  dom.window.CodexCapacity.mount(host, ['gpt-test']);
  await new Promise(resolve => setImmediate(resolve));
  return host;
}

test('first delta is visible while generation is still running', async t => {
  const host = await renderRow(t, { model: 'gpt-test', status: 'running',
    output_observation: { kind: 'text_delta', elapsed_seconds: 1.234 } });
  assert.equal(host.querySelector('.capacity-latency').textContent, '1.23s');
  assert.match(host.textContent, /已收到正文，继续输出中/);
});

test('legacy completed-message observation never becomes TTFT', async t => {
  const host = await renderRow(t, { model: 'gpt-test', status: 'running',
    output_observation: { kind: 'message_completed', elapsed_seconds: 41 } });
  assert.equal(host.querySelector('.capacity-latency').textContent, '—');
  assert.match(host.textContent, /已收到完整消息/);
});

test('finished stream uses persisted TTFT and describes text timing', async t => {
  const host = await renderRow(t, { model: 'gpt-test', status: 'success', transport: 'codex-app-server',
    metrics: { ttft_seconds: 1.234, end_to_end_tokens_per_second: 35 } });
  assert.equal(host.querySelector('.capacity-latency').textContent, '1.23s');
  assert.match(host.textContent, /第一个非空正文增量/);
  assert.doesNotMatch(host.textContent, /未提供逐 token 时序/);
});
