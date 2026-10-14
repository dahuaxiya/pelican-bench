/** Local VPN measurements; selector mutation stays in the backend ownership lease. */
(function () {
  'use strict';
  const $ = id => document.getElementById('network-' + id);
  if (!$('form')) { return; }
  let visible = false, initialized = false, timer, active = null, selected = null, displayed = null, groups = [], page = 1, sequence = 0, submitting = false, applying = false, pollError = '', rendered = '';
  let draftChanged = false, modelsLoaded = false;
  const states = { idle: '暂无测试', running: '测试中', stopping: '停止并恢复中', completed: '已完成', cancelled: '已停止', failed: '未完成', interrupted: '已中断' };
  const stages = { proxy_tcp: '本机代理', proxy_connect: '代理隧道', route: '路由核对', tls: 'TLS', websocket: '官方握手' };

  /** Load account models; external-plan restoration must keep its explicit timeout. */
  async function changeMode(resetTimeout = true) {
    const modelMode = $('mode-model').checked;
    $('model-options').hidden = !modelMode; $('model').required = modelMode;
    $('timeout').max = modelMode ? '600' : '60';
    if (resetTimeout) { $('timeout').value = modelMode ? '180' : '12'; }
    if (!modelMode || modelsLoaded) { return; }
    try {
      const catalog = await request('/models');
      const previous = $('model').value;
      $('model').replaceChildren(el('option', '请选择模型')); $('model').firstChild.value = '';
      for (const model of catalog.models || []) {
        const option = el('option', model); option.value = model; $('model').append(option);
      }
      modelsLoaded = !!catalog.ready;
      $('model').value = catalog.models?.includes(previous) ? previous : catalog.models?.includes('gpt-6.1-sol') ? 'gpt-6.1-sol' : catalog.models?.[0] || '';
    } catch (e) { error(e.message); }
  }

  /** Adopt a new external running plan only while the user has no form draft. */
  function restoreRunningPlan(value) {
    if (active?.id === value.id || draftChanged || !value.options || !['running', 'stopping'].includes(value.status)) { return; }
    const options = value.options, modelMode = options.mode === 'model';
    $('mode-model').checked = modelMode; $('mode-handshake').checked = !modelMode;
    $('model-options').hidden = !modelMode; $('model').required = modelMode;
    $('timeout').max = modelMode ? '600' : '60';
    for (const key of ['top', 'repeats', 'timeout', 'concurrency']) { if (options[key] != null) { $(key).value = options[key]; } }
    if (modelMode) {
      if (!Array.from($('model').options).some(option => option.value === options.model)) {
        const option = el('option', options.model); option.value = options.model; $('model').append(option);
      }
      $('model').value = options.model; $('sort').value = options.sort_by || 'ttft';
      // Loading the catalog also keeps other models selectable after this job ends.
      changeMode(false);
    }
  }

  /** Refresh elapsed waiting time without rebuilding the ranking on every tick. */
  function progress(value) {
    const modelMode = value.options?.mode === 'model';
    const phase = value.phase === 'https' ? 'HTTPS 初筛 ' + value.screened + '/' + value.screen_total :
      value.phase === 'restoring' ? '恢复原节点' : (value.done || 0) + '/' + (value.total || 0) + (modelMode ? ' 次生成' : ' 次握手');
    const waiting = modelMode && value.current_node && Number.isFinite(value.current_started_at) ?
      ' · ' + (value.output_observation ? '生成中' : '等待首字') + ' ' + Math.max(0, Math.floor(Date.now() / 1000 - value.current_started_at)) + 's' : '';
    $('progress').textContent = value.id ? states[value.status] + ' · ' + phase + ' · ' + value.id +
      (value.options?.concurrency ? ' · 并发 ' + value.options.concurrency + (['running', 'stopping'].includes(value.status) && value.phase !== 'https' ? ' · 运行 ' + (value.running || 0) + ' · 排队 ' + (value.queued || 0) : '') : '') +
      (modelMode ? ' · ' + value.options.model + ' · ' + (value.options.sort_by === 'throughput' ? '吞吐优先' : '首字优先') : '') +
      (value.current_node ? ' · ' + value.current_node + ' 第 ' + value.round + ' 次' : '') + waiting +
      (modelMode && value.current_node && value.output_observation ? ' · 已出首字 ' + ms(value.output_observation.elapsed_seconds * 1000) : '') : '暂无测试';
  }

  /** Build text-only DOM so node names and controller errors cannot become HTML. */
  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text != null) { node.textContent = String(text); }
    if (cls) { node.className = cls; }
    return node;
  }

  /** Read local JSON; don't replace existing results when one poll fails. */
  async function request(path, body) {
    const response = await fetch('/api/network' + path, body === undefined ? { cache: 'no-store' } : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok || result.error) { throw new Error(result.error || '读取节点测试失败'); }
    return result;
  }

  /** Keep a recoverable error beside its controls rather than hiding it in a toast. */
  function error(message = '') { $('error').textContent = message; $('error').hidden = !message; }

  /** Save only explicit form values; a blank password preserves the existing secret. */
  async function configure() {
    const values = { controller: $('controller').value.trim(), proxy: $('proxy').value.trim(), group: $('group').value };
    if ($('secret').value) { values.secret = $('secret').value; }
    const settings = await request('/config', values);
    $('secret').value = '';
    $('secret').placeholder = settings.secret_set ? '已保存，留空保持' : '未设置';
  }

  /** Refresh live group metadata only on demand; polling must not reset form choices. */
  async function discover() {
    error(); $('discover').disabled = true; $('message').textContent = '正在读取本机代理…';
    try {
      await configure();
      const result = await request('/discover', {});
      groups = result.groups;
      const saved = result.settings.group;
      $('group').replaceChildren(el('option', '请选择实际经过的组'));
      $('group').firstChild.value = '';
      for (const group of groups) {
        const option = el('option', group.name + ' · ' + group.nodes.length + ' 个节点');
        option.value = group.name; $('group').append(option);
      }
      $('group').value = groups.some(g => g.name === saved) ? saved : result.suggested_group;
      route();
      $('message').textContent = result.auth_ready ? '已读取节点，ChatGPT 登录态可用' : '已读取节点；请先到设置登录 ChatGPT';
    } catch (e) { error(e.message); $('message').textContent = ''; }
    finally { controls(); }
  }

  /** Show the currently selected group metadata, without claiming route verification. */
  function route() {
    const group = groups.find(g => g.name === $('group').value);
    $('route').textContent = group ? '当前选择：' + group.current : '';
  }

  /** Disable conflicting commands until cancellation and restoration have both ended. */
  function controls() {
    const switching = applying || ['running', 'stopping'].includes(active?.application?.status);
    const busy = switching || ['running', 'stopping'].includes(active?.status);
    $('start').disabled = submitting || busy;
    $('start').textContent = submitting ? '提交中…' : switching ? '等待节点切换…' : busy ? '测试中…' : '开始测速';
    const symbol = window.BenchUI?.icon('play'); if (symbol) { $('start').prepend(symbol); }
    $('cancel').hidden = !busy || applying;
    $('cancel').disabled = active?.status === 'stopping' || active?.application?.status === 'stopping';
    $('discover').disabled = busy || submitting;
    $('form').querySelectorAll('input, select').forEach(input => { input.disabled = busy || submitting; });
    $('apply').disabled = busy || submitting || displayed?.status !== 'completed' || !displayed?.recommendations?.length;
    $('apply').textContent = switching ? '切换并验证中…' : '切换最优节点';
    const applyIcon = window.BenchUI?.icon('arrow-right-left'); if (applyIcon) { $('apply').prepend(applyIcon); }
    $('apply').title = displayed?.recommendations?.[0] ? '切换到：' + displayed.recommendations[0] : '先完成节点测速';
    $('recover').disabled = busy || submitting;
  }

  /** Apply exactly the visible ranking; polling must not change the click's target. */
  async function applyBest() {
    if ($('apply').disabled || !displayed?.id) { return; }
    const runId = displayed.id;
    sequence++;
    applying = true; controls(); error();
    $('application').textContent = '正在切换并验证官方连接…';
    try {
      active = await request('/apply', { run_id: runId });
      applying = false;
      selected = runId; sequence++; render(active);
    } catch (e) {
      error(e.message); $('application').textContent = '切换未提交：' + e.message;
    } finally { applying = false; controls(); schedule(); }
  }

  /** Start one batch and immediately show acknowledged progress; never start on page load. */
  async function start(event) {
    event.preventDefault(); if (submitting) { return; }
    submitting = true; controls(); error(); $('message').textContent = '正在提交测试…';
    try {
      await configure();
      const options = { mode: $('mode-model').checked ? 'model' : 'handshake', top: Number($('top').value), repeats: Number($('repeats').value), timeout: Number($('timeout').value), concurrency: Number($('concurrency').value) };
      if (options.mode === 'model') { options.model = $('model').value; options.sort_by = $('sort').value; }
      active = await request('/run', options);
      selected = null; render(active); $('message').textContent = '批次 ' + active.id + ' 已开始';
      await history();
    } catch (e) { error(e.message); $('message').textContent = ''; }
    finally { submitting = false; controls(); schedule(); }
  }

  /** Format absent observations as unknown, never as a zero-latency winner. */
  function ms(value) { return Number.isFinite(value) ? Math.round(value) + ' ms' : '—'; }

  /** Keep unknown token usage distinct from a measured zero or slow response. */
  function speed(value) { return Number.isFinite(value) ? value.toFixed(2) + ' token/s' : '—'; }

  /** Update only result regions, preserving details and page scroll across polling. */
  function render(value) {
    displayed = value;
    progress(value);
    const serialized = JSON.stringify(value);
    if (serialized === rendered) { return; }
    rendered = serialized;
    const modelMode = value.options?.mode === 'model';
    $('result-title').textContent = modelMode ? '模型实测排名' : '官方握手排名';
    $('metric-note').textContent = modelMode ? '首字包含启动、节点建连等待、排队和推理。端到端吞吐 = 上游输出 token 数（含推理）÷ 完整响应耗时。并发共享本机带宽和账号额度；单节点独占测速可将并发设为 1。缺失用量不估算速度。' :
      '测量认证 WebSocket 握手，不发送模型问题。波动为成功样本的最大与最小延迟之差；排名优先看成功率。';
    const focusedName = document.activeElement?.closest('#network-rank details')?.dataset.name;
    $('bar').hidden = !['running', 'stopping'].includes(value.status);
    $('bar').max = value.phase === 'https' ? value.screen_total || 1 : value.total || 1;
    $('bar').value = value.phase === 'https' ? value.screened || 0 : value.done || 0;
    const recommendations = value.recommendations || [];
    $('recommendation').textContent = recommendations.length ? '建议主用：' + recommendations[0] +
      (recommendations[1] ? '　备用：' + recommendations[1] : '') : value.status === 'completed' ? '本轮没有全部成功且指标完整的候选节点' : '';
    const restoration = value.restoration || {};
    const restoreText = { pending: '测试结束后恢复原节点', restored: '测速结束时已恢复：' + restoration.node,
      external_change: '已保留外部选择：' + restoration.node, failed: '原节点恢复失败，请检查 VPN 后重试', not_needed: '' };
    $('restore').textContent = [restoreText[restoration.state] || '', value.failure].filter(Boolean).join('。');
    const application = value.application || {}, recovery = application.restoration || {};
    const appliedText = { running: '正在切换并验证官方连接：' + application.target,
      stopping: '正在取消切换并恢复原节点…', succeeded: '已切换：' + application.target + ' · 官方连接验证通过 · ' + new Date(application.finished_at * 1000).toLocaleString(),
      failed: application.failure || '切换失败', cancelled: '已取消切换', interrupted: '上次切换已中断' };
    const recoveryText = { restored: '已恢复点击前的节点：' + recovery.node, external_change: '保留外部选择：' + recovery.node,
      failed: '节点恢复未确认，请点击恢复原节点', not_needed: '', retained: '' };
    if (!applying) {
      $('application').textContent = [appliedText[application.status], recoveryText[recovery.state]].filter(Boolean).join('。');
    }
    $('recover').hidden = restoration.state !== 'failed' && value.status !== 'interrupted' && recovery.state !== 'failed' && recovery.state !== 'pending';
    const opened = new Set(Array.from($('rank').querySelectorAll('details[open]')).map(d => d.dataset.name));
    const wrap = el('div', null, 'network-table-wrap'), table = el('table', null, 'network-table' + (modelMode ? ' network-model-table' : ''));
    const head = el('thead'), heading = el('tr');
    (modelMode ? ['节点', '成功 / 尝试', '首字中位耗时', '端到端吞吐', '总耗时中位数'] :
      ['节点', '成功 / 尝试', '中位延迟', '最慢成功', '波动']).forEach(label => heading.append(el('th', label)));
    head.append(heading); table.append(head);
    const body = el('tbody'), rows = value.rows || [], maximum = Math.max(1, ...rows.map(r => r.median_ms || 0));
    for (const row of rows) {
      const line = el('tr'), name = el('td'), details = el('details');
      details.dataset.name = row.name; details.open = opened.has(row.name);
      details.append(el('summary', (row.rank ? row.rank + '. ' : '') + row.name));
      const samples = el('ul');
      row.samples.forEach((s, index) => {
        const metrics = s.metrics || {};
        let description = s.ok ? ms(s.elapsed_ms) : (stages[s.stage] || s.stage) + ' · ' + (s.status ? 'HTTP ' + s.status : s.error) + ' · ' + ms(s.elapsed_ms);
        if (modelMode && s.ok) {
          description = '首字 ' + ms(metrics.ttft_seconds == null ? null : metrics.ttft_seconds * 1000) + ' · ' + speed(metrics.end_to_end_tokens_per_second) +
            ' · 总耗时 ' + ms(s.elapsed_ms) + ' · 输出 ' + (metrics.output_tokens ?? '未知') + ' token · 缓存输入 ' + (metrics.cached_input_tokens ?? '未知') + ' token';
        }
        samples.append(el('li', (index + 1) + '. ' + description, s.ok ? '' : 'network-failed'));
      });
      details.append(samples); name.append(details); line.append(name);
      if (row.state) {
        const rowState = { queued: '排队中', running: modelMode ? (row.output_observation ? '生成中' : '等待首字') : '握手中', completed: '本轮完成', stopped: '已停止' };
        name.append(el('small', (rowState[row.state] || row.state) + (row.round ? ' · 第 ' + row.round + ' 次' : '') +
          (row.state === 'running' && row.output_observation ? ' · 首字 ' + ms(row.output_observation.elapsed_seconds * 1000) : ''), 'network-muted'));
      }
      line.append(el('td', row.success + ' / ' + row.attempts + (row.complete ? '' : '（未满）')));
      if (modelMode) {
        line.append(el('td', ms(row.median_ttft_ms)), el('td', speed(row.median_tokens_per_second)), el('td', ms(row.median_ms)));
        if (row.metric_samples !== row.success) { name.append(el('small', '部分样本缺少首字或有效用量，不排名', 'network-muted')); }
        body.append(line); continue;
      }
      const latency = el('td', ms(row.median_ms));
      if (row.median_ms != null) {
        const track = el('span', null, 'network-latency'), bar = el('span');
        bar.style.width = Math.min(100, row.median_ms / maximum * 100) + '%'; track.append(bar); latency.append(track);
      }
      line.append(latency, el('td', ms(row.max_success_ms)), el('td', ms(row.spread_ms))); body.append(line);
    }
    table.append(body); wrap.append(table); $('rank').replaceChildren(wrap);
    // Preserve keyboard focus on the same sample disclosure when new progress
    // arrives. preventScroll avoids snapping back up during a long ranking.
    if (focusedName) {
      Array.from($('rank').querySelectorAll('details')).find(d => d.dataset.name === focusedName)?.querySelector('summary')?.focus({ preventScroll: true });
    }
    const screening = el('div');
    (value.screening || []).forEach(row => screening.append(el('p', row.name + ' · ' + (row.delay_ms == null ? '不可用' : ms(row.delay_ms)), 'network-muted')));
    $('screen-results').replaceChildren(screening);
    controls();
  }

  /** Read one immutable batch on explicit selection, leaving a live job running. */
  async function show(runId) {
    selected = runId; const version = ++sequence;
    try {
      const result = await request('/run/' + encodeURIComponent(runId));
      if (version === sequence) { render(result); }
    } catch (e) { error(e.message); }
  }

  /** Fetch a bounded page; history buttons never submit another measurement. */
  async function history() {
    const expectedPage = page, data = await request('/history?page=' + page);
    if (page !== expectedPage) { return; }
    $('page').textContent = page + ' / ' + Math.max(1, Math.ceil(data.total / 20));
    $('prev').disabled = page <= 1; $('next').disabled = page * 20 >= data.total;
    const list = document.createDocumentFragment();
    data.items.forEach(item => {
      const row = el('div', null, 'network-history-item'), info = el('div', new Date(item.created_at * 1000).toLocaleString());
      info.append(el('small', item.id));
      info.append(el('small', item.options?.mode === 'model' ? '模型实测 · ' + item.options.model : '握手测速'));
      const button = el('button', '查看', 'secondary-action'); button.type = 'button';
      button.addEventListener('click', () => show(item.id));
      row.append(info, el('span', states[item.status]), el('p', item.recommendations?.[0] || '暂无推荐'), button); list.append(row);
    });
    $('history-list').replaceChildren(list);
  }

  /** Keep exactly one polling timer; hidden pages do not keep issuing requests. */
  function schedule() { clearTimeout(timer); if (visible) { timer = setTimeout(poll, 1500); } }

  /** Poll active status without replacing the form or stealing history selection. */
  async function poll() {
    if (!visible) { return; }
    const version = sequence;
    try {
      const result = await request('');
      // A slow pre-click poll must not overwrite the newly acknowledged switch.
      if (version !== sequence) { return; }
      // Clear only an error owned by polling; a rejected user action must stay
      // visible even when the next unrelated status request succeeds.
      if (pollError && $('error').textContent === pollError) { error(); }
      pollError = '';
      const changed = active?.status !== result.status || active?.id !== result.id;
      restoreRunningPlan(result);
      active = result;
      if (version === sequence && (!selected || selected === result.id)) { render(result); }
      controls(); if (changed) { await history(); }
    } catch (e) { pollError = e.message; error(e.message); }
    finally { schedule(); }
  }

  /** Load persisted form settings once per page lifetime, preserving subsequent edits. */
  async function enter() {
    try {
      if (!initialized) {
        const settings = await request('/config');
        $('controller').value = settings.controller; $('proxy').value = settings.proxy;
        $('secret').placeholder = settings.secret_set ? '已保存，留空保持' : '未设置';
        if (settings.group) {
          const option = el('option', settings.group); option.value = settings.group;
          $('group').append(option); $('group').value = settings.group;
        }
        initialized = true;
      }
      await history(); await poll();
    } catch (e) { error(e.message); }
  }

  $('form').addEventListener('submit', start);
  $('form').addEventListener('input', () => { draftChanged = true; });
  $('form').addEventListener('change', () => { draftChanged = true; });
  $('mode-handshake').addEventListener('change', changeMode);
  $('mode-model').addEventListener('change', changeMode);
  $('apply').addEventListener('click', applyBest);
  $('discover').addEventListener('click', discover);
  $('group').addEventListener('change', route);
  $('live').addEventListener('click', () => { selected = null; sequence++; poll(); });
  $('cancel').addEventListener('click', async () => {
    if (!active?.id) { return; }
    $('cancel').disabled = true; $('message').textContent = '正在停止并恢复原节点…';
    try { active = await request('/cancel', { run_id: active.id }); controls(); }
    catch (e) { error(e.message); controls(); }
    schedule();
  });
  $('recover').addEventListener('click', async () => {
    $('recover').disabled = true;
    try {
      const result = await request('/recover', {});
      $('message').textContent = result.state === 'external_change' ? '保留外部选择：' + result.node : result.state === 'restored' ? '已恢复：' + result.node : result.state === 'retained' ? '已确认保留：' + result.node : '没有待恢复的节点';
      if (selected) { await show(selected); } else { await poll(); }
    } catch (e) { error(e.message); }
    finally { $('recover').disabled = false; }
  });
  $('prev').addEventListener('click', () => { page--; history().catch(e => error(e.message)); });
  $('next').addEventListener('click', () => { page++; history().catch(e => error(e.message)); });
  document.addEventListener('view-change', event => {
    visible = event.detail === 'network'; clearTimeout(timer);
    if (visible) { enter(); }
  });
}());
