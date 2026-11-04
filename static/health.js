/** Saved local checks never run on page load; only the explicit form submits a probe. */
(function () {
  'use strict';
  const $ = id => document.getElementById('health-' + id);
  if (!$('form')) { return; }
  const environments = { company: '公司', home: '家里', other: '其他' };
  const states = { ok: '通过', warning: '警告', fail: '失败', skipped: '跳过', unknown: '未知',
    success: '成功', failed: '失败', cancelled: '已停止', interrupted: '已中断', completed: '已结束', running: '检查中', stopping: '停止中' };
  let visible = false, loading = false, submitting = false, timer, current = null, catalog = null;
  let pending = null, historyKey = '', reportSequence = 0, comparisonSequence = 0, mutation = 0;
  let renderedId = '', renderedKey = '', comparisonKey = '', records = [], comparisonInitialized = false;
  const disclosureStates = new Map();
  let previewImage = null, previewKey = '';

  /** Text-only DOM keeps persisted model names and route metadata out of HTML parsing. */
  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text != null) { result.textContent = text; }
    if (className) { result.className = className; }
    return result;
  }
  /** Use the same bundled icons as navigation; text remains readable without the library. */
  function icon(name, className = '') {
    const result = window.BenchUI?.icon(name) || node('span');
    if (className) { result.classList.add(className); }
    result.setAttribute('aria-hidden', 'true');
    return result;
  }
  /** Only explicit warning/failure statuses count as issues, not missing results. */
  function isIssue(check) { return ['warning', 'fail'].includes(check.status); }
  /** Treat stopping as active until cancellation is acknowledged by the backend. */
  function isActive(run) { return ['running', 'stopping'].includes(run?.status); }
  /** Keep a missing date visibly unknown rather than showing an invalid timestamp. */
  function timestamp(value) {
    return Number.isFinite(value) ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '时间未知';
  }
  /** Keep a failed read visible without erasing the last successfully loaded result. */
  function error(message = '') { $('error').textContent = message; $('error').hidden = !message; }
  /** Request local JSON only; timeout does not imply that a submitted job was cancelled. */
  async function request(path, body) {
    const response = await fetch('/api/health-check' + path, {
      cache: 'no-store', signal: AbortSignal.timeout?.(25000),
      ...(body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    });
    const value = await response.json();
    if (!response.ok) { throw new Error(value.error || '读取健康检查失败'); }
    return value;
  }
  /** Preserve missing durations as unknown, never as zero latency. */
  function duration(value) { return Number.isFinite(value) ? value.toFixed(2) + ' s' : '未知'; }
  /** Render the viewer's local timestamp with a stable historical ID suffix. */
  function label(run) {
    return (environments[run.environment] || '其他') + ' · ' + timestamp(run.started_at) +
      ' · ' + (run.model_check ? run.model : '仅连接检查') + ' · ' + run.id.slice(0, 6);
  }
  /** Authentication source is not account identity and cannot establish equal quotas. */
  function auth(value) { return ({ site: '本站登录账号', local: '本机 Codex 登录账号' })[value] || '未知'; }
  /** A proxy address is configuration evidence, not proof of an exit IP or active VPN node. */
  function route(value) {
    if (!value) { return '未知'; }
    const parts = Object.entries(value).filter(([key]) => key !== 'bypass_configured').map(([key, text]) => key + ': ' + text);
    return (parts.join('；') || '未设置显式代理（仍可能使用 TUN/VPN）') + (value.bypass_configured ? '；有代理绕过规则' : '');
  }
  /** Build a scrollable table, including on narrow screens, without injecting stored HTML. */
  function table(headers, rows) {
    const wrap = node('div', null, 'health-table-wrap'), grid = node('table', null, 'health-table');
    const head = node('thead'), heading = node('tr'), body = node('tbody');
    headers.forEach(text => { const cell = node('th', text); cell.scope = 'col'; heading.append(cell); });
    head.append(heading);
    rows.forEach(values => {
      const row = node('tr');
      row.dataset.different = String(values[1] !== values[2]);
      values.forEach((value, index) => {
        const cell = node(index === 0 ? 'th' : 'td');
        if (index === 0) { cell.scope = 'row'; }
        cell.textContent = String(value ?? '未知'); row.append(cell);
      });
      body.append(row);
    });
    grid.append(head, body); wrap.append(grid); return wrap;
  }
  /** Only successful complete requests contribute timings; absent usage never becomes zero speed. */
  function measurements(run, field) {
    return (run.samples || []).filter(row => row.status === 'success').map(row =>
      field === 'duration' ? row.duration_seconds : row.metrics?.[field]
    ).filter(value => Number.isFinite(value) && value >= 0).sort((a, b) => a - b);
  }
  /** Use a median for repeated samples, retaining single-sample values without extrapolation. */
  function median(values) {
    const middle = Math.floor(values.length / 2);
    return !values.length ? null : values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
  }
  /** Label end-to-end throughput explicitly; it is not a measurement of pure token generation. */
  function speed(value) { return Number.isFinite(value) ? value.toFixed(2) + ' tokens/s' : '—'; }
  /** Count SVG attempts separately from text speed medians.
   * @param {object} run Saved or active check.
   * @return {Array} Finished model attempts, excluding queued or skipped SVG work.
   */
  function attempts(run) {
    const rows = [...(run.samples || [])];
    const svg = run.svg_result;
    if (run.svg_check && (['success', 'failed'].includes(svg?.status) || (svg?.status === 'cancelled' && svg.started_at))) { rows.push(svg); }
    return rows;
  }
  /** Highlight request usability and three observed speed metrics, not machine diagnostic counts. */
  function performance(run) {
    const samples = run.samples || [], succeeded = samples.filter(row => row.status === 'success').length;
    const requested = attempts(run), passed = requested.filter(row => row.status === 'success').length;
    $('success-count').textContent = requested.length ? passed + ' / ' + requested.length : '—';
    $('success-count').dataset.state = !requested.length ? 'unknown' : passed === requested.length ? 'success' : passed ? 'warning' : 'failed';
    $('success-note').textContent = run.model_check ? '计划 ' + run.repeats + ' 次文字' + (run.svg_check ? ' + 1 次 SVG' : '') : '未发送模型请求';
    const fields = [['ttft', 'ttft_seconds'], ['speed', 'end_to_end_tokens_per_second'], ['total', 'duration']];
    fields.forEach(([id, field]) => {
      const values = measurements(run, field), value = median(values);
      $(id).textContent = id === 'speed' ? speed(value) : value == null ? '—' : duration(value);
      $(id).title = `成功样本中 ${values.length} 次有有效数据；多次取中位数`;
      let note = values.length ? (values.length > 1 ? '中位数 · ' + values.length + ' 次' : '1 次实测') : !samples.length ? '尚无结果' : !succeeded ? '无成功样本' : '未取得数据';
      if (id === 'speed') {
        if (values.length) { note += ' · 含等待与推理'; }
        else if (succeeded) { note = run.sample_mode !== 'performance' ? '旧记录未测速' : samples.some(row => row.metrics?.sample_short) ? '样本过短' : '缺少准确用量或时序'; }
      }
      if (run.svg_check && values.length) { note = '文字测速 · ' + note; }
      $(id + '-note').textContent = note;
    });
  }
  /** Lead with observed model availability; local warnings cannot override the capacity verdict. */
  function summary(run) {
    const verdict = run.verdict || {}, active = isActive(run);
    let title = '尚未验证模型可用性', tone = 'unknown', symbol = 'circle-help';
    if (verdict.code === 'capacity') { title = verdict.success ? '请求已恢复，曾遇容量不足' : '已复现模型容量不足'; tone = 'warning'; symbol = 'triangle-alert'; }
    else if (verdict.code === 'quota') { title = '账号额度受限'; tone = 'warning'; symbol = 'circle-alert'; }
    else if (verdict.code === 'passed' && run.status === 'completed') { title = run.svg_check ? '文字与 SVG 生成均通过' : '本轮模型可用'; tone = 'success'; symbol = 'circle-check'; }
    else if (verdict.code === 'inconclusive') { title = '模型请求未全部成功'; tone = 'warning'; symbol = 'circle-alert'; }
    if (active) { title = run.status === 'stopping' ? '正在停止检测' : run.phase === '补充本机诊断' ? '模型检查结束，正在补充诊断' : run.svg_result?.status === 'running' ? '正在生成 SVG' : '正在连接模型并测速'; tone = 'running'; symbol = 'activity'; }
    else if (['cancelled', 'interrupted', 'failed'].includes(run.status)) {
      title = { cancelled: '检测已停止', interrupted: '检测已中断', failed: '检测未完成' }[run.status];
      tone = run.status === 'failed' ? 'failed' : 'unknown'; symbol = 'circle-pause';
    }
    $('summary').hidden = false; $('summary').dataset.tone = tone;
    $('summary-icon').replaceChildren(icon(symbol)); $('result-title').textContent = title;
    $('report-meta').textContent = (environments[run.environment] || '其他') + ' · ' + timestamp(run.started_at) + (run.model_check ? ' · ' + run.model : '');
    $('verdict').textContent = verdict.text || '尚未取得模型实测结论';
    performance(run);
    const next = $('next-step'); next.hidden = active;
    if (active) { return; }
    let advice;
    if (verdict.code === 'capacity') { advice = '保持账号和模型一致，在另一网络复测后对比。单次容量提示不能确定是公司网络导致。'; }
    else if (verdict.code === 'quota') { advice = '先核对账号用量和重置时间。切换网络不能据此保证恢复。'; }
    else if (run.model_blocked) { advice = run.model_blocked; }
    else if (['cancelled', 'interrupted', 'failed'].includes(run.status)) { advice = '本轮检查未完成；已有结果仅代表已完成的部分，仍需重新体检。'; }
    else if (verdict.code === 'passed') {
      if (run.sample_mode !== 'performance') { advice = '这是一条旧版连通性记录，未测输出吞吐。重新检测会使用生成请求测量速度。'; }
      else if ((run.doctor?.checks || []).some(isIssue)) { advice = '模型请求已成功，本机诊断另有告警，可展开查看；不影响本轮请求成功的结论。'; }
      else { next.hidden = true; return; }
    } else if (verdict.code === 'not_tested') { advice = '尚未发送模型请求。连接检查不能证明模型可用，需要启用模型请求与测速后重新检测。'; }
    else { advice = '查看请求错误；需要排查连接时可勾选附加本机诊断。不要仅凭失败认定是容量不足或公司网络导致。'; }
    next.replaceChildren(node('strong', '下一步'), node('p', advice));
  }
  /** Build one expandable group, with problems open on first view and normal checks collapsed. */
  function checkGroup(key, title, symbol, rows) {
    const group = node('details', null, 'health-group'), heading = node('summary');
    group.dataset.healthDisclosure = key;
    const issues = rows.filter(isIssue).length, unknown = rows.filter(row => row.status !== 'ok' && !isIssue(row)).length;
    group.open = issues > 0 || unknown > 0;
    heading.append(icon(symbol), node('span', title), node('span', issues ? issues + ' 项需关注' : unknown ? unknown + ' 项未确认' : rows.length + ' 项通过', 'health-group-count'), icon('chevron-down', 'health-chevron'));
    group.append(heading);
    const passed = rows.filter(row => row.status === 'ok');
    const normal = node('details', null, 'health-passed-checks'); normal.dataset.healthDisclosure = 'passed:' + key;
    normal.append(node('summary', passed.length + ' 项已通过'));
    // In mixed groups expose the problem first; healthy rows remain available without
    // forcing the user to scan installation and disk checks before the relevant error.
    rows.forEach(row => {
      const item = node('div', null, 'health-check-row'), body = node('div', null, 'health-check-body');
      item.dataset.state = row.status || 'unknown';
      const name = node('div', null, 'health-check-heading'); name.append(node('strong', row.title), node('span', states[row.status] || '未知')); body.append(name);
      if (row.reason) { body.append(node('p', row.reason)); }
      if (row.advice && isIssue(row)) { body.append(node('p', row.advice, 'health-check-advice')); }
      if (row.provider === 'other') { body.append(node('p', '目标是其他提供方，此项不代表官方通道。', 'health-check-advice')); }
      if (row.facts?.length || row.provider) {
        const details = node('details', null, 'health-check-evidence'); details.dataset.healthDisclosure = 'evidence:' + row.id;
        details.append(node('summary', '检查依据'));
        (row.facts || []).forEach(fact => details.append(node('p', fact)));
        if (row.provider) { details.append(node('p', row.provider === 'openai' ? '检查目标：OpenAI' : '检查目标：其他提供方')); }
        body.append(details);
      }
      item.append(icon(row.status === 'ok' ? 'circle-check' : isIssue(row) ? 'circle-alert' : 'circle-help'), body,
        node('span', duration(Number.isFinite(row.duration_ms) ? row.duration_ms / 1000 : null), 'health-check-time'));
      if ((issues || unknown) && row.status === 'ok') { normal.append(item); } else { group.append(item); }
    });
    if ((issues || unknown) && passed.length) { group.append(normal); }
    return group;
  }
  /** Keep the actual request evidence separate from CLI diagnostics and their potentially different path. */
  function modelResults(run) {
    const aside = node('section', null, 'health-model-results'); aside.setAttribute('aria-label', '模型请求明细');
    const requests = node('details', null, 'health-requests'); requests.dataset.healthDisclosure = 'requests';
    requests.open = (run.samples || []).some(row => row.status !== 'success');
    requests.append(node('summary', '请求明细 · ' + (run.samples?.length || 0) + ' / ' + (run.model_check ? run.repeats : 0) + ' 次'));
    requests.append(node('p', (run.model_check ? run.model : '未启用') + ' · ' + auth(run.auth_source), 'health-model-name'));
    (run.samples || []).forEach(row => {
      const sample = node('div', null, 'health-sample'), heading = node('div', null, 'health-sample-heading');
      const state = node('span', states[row.status] || '未知', 'health-status'); state.dataset.state = row.status;
      heading.append(node('span', '第 ' + row.number + ' 次请求'), state); sample.append(heading);
      if (row.capacity_detected) { sample.append(node('p', '曾出现模型容量不足')); }
      if (row.reason) { sample.append(node('p', row.reason)); }
      const events = [...new Set((row.events || []).map(event => event.category).filter(Boolean))];
      if (events.length) { sample.append(node('p', events.join(' · '))); }
      const times = node('div', null, 'health-sample-times');
      for (const [title, value] of [['首字耗时', row.metrics?.ttft_seconds], ['总耗时', row.duration_seconds]]) {
        const metric = node('span', title); metric.append(node('strong', duration(value))); times.append(metric);
      }
      const throughput = node('span', '输出吞吐'); throughput.append(node('strong', row.status === 'success' ? speed(row.metrics?.end_to_end_tokens_per_second) : '—'));
      const tokens = node('span', '输出 token'); tokens.append(node('strong', Number.isFinite(row.metrics?.output_tokens) ? row.metrics.output_tokens : '未知'));
      times.append(throughput, tokens);
      sample.append(times); requests.append(sample);
    });
    if (!run.samples?.length) { requests.append(node('p', run.model_check ? '尚无模型请求结果' : '未发送模型请求', 'muted')); }
    aside.append(requests);
    const context = node('details', null, 'health-context'), facts = node('dl', null, 'health-facts');
    context.dataset.healthDisclosure = 'context'; context.append(node('summary', '账号与连接详情'));
    for (const [key, value] of [['记录 ID', run.id], ['状态', states[run.status] || '未知'],
      ['检测总时长', duration(Number.isFinite(run.finished_at) && Number.isFinite(run.started_at) ? run.finished_at - run.started_at : null)],
      ['账号来源', auth(run.auth_source)], ['CLI 版本', run.cli_version || '未知'],
      ['本机 CLI 代理', route(run.route?.cli)], ['本站请求代理', route(run.route?.probe)]]) { facts.append(node('dt', key), node('dd', value)); }
    context.append(facts, node('p', '代理配置不等于实际出口 IP；账号来源不等于账号身份。', 'health-evidence-note')); aside.append(context);
    context.append(node('p', '本站模型实测与本机 CLI 检查可能走不同路径，结果不代表当前 Codex 桌面会话。', 'health-evidence-note'));
    return aside;
  }
  /** Render generated content only as an image, never as active HTML in the workspace.
   * The server sanitizes and bounds SVG; browser image context additionally prevents
   * scripts, parent CSS changes and external resource loads. Reuse its node across
   * progress updates so polling does not reload the animation.
   * @param {object} run Saved or active check; legacy records may lack SVG fields.
   * @return {HTMLElement} Unframed result section with an inert image preview.
   */
  function svgResult(run) {
    const result = run.svg_result || {}, section = node('section', null, 'health-svg');
    section.setAttribute('aria-label', 'SVG 生成结果');
    const heading = node('div', null, 'health-svg-heading'), status = node('span', null, 'health-status');
    status.dataset.state = result.status || 'unknown';
    status.textContent = !run.svg_check ? '未检测' : result.status === 'queued' ? '等待生成' : result.status === 'running' ? '生成中' : states[result.status] || '未知';
    heading.append(node('h4', 'SVG 生成'), status); section.append(heading);
    if (!run.svg_check) { return section; }
    if (result.reason) { section.append(node('p', result.reason, 'health-notice')); }
    const events = [...new Set((result.events || []).map(event => event.category).filter(Boolean))];
    if (events.length) { section.append(node('p', events.join(' · '), 'health-notice')); }
    const body = node('div', null, 'health-svg-body');
    if (result.status === 'success' && typeof result.svg === 'string' && result.svg.length <= 128 * 1024) {
      const key = run.id + '\n' + result.svg;
      if (key !== previewKey) {
        previewImage = node('img'); previewImage.alt = '模型生成的 SVG 图';
        previewImage.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(result.svg);
        previewImage.id = 'health-svg-image'; previewKey = key;
        const image = previewImage;
        image.addEventListener('error', () => {
          // Structure validation cannot prove browser rendering. Surface a broken image
          // without changing the stored API result or automatically spending quota again.
          image.hidden = true;
          image.dataset.renderFailed = 'true';
          if (image !== previewImage) { return; }
          $('svg-render-error')?.remove();
          const warning = node('p', 'SVG 已生成，但浏览器无法显示此图。', 'health-notice');
          warning.id = 'health-svg-render-error'; image.parentElement?.append(warning);
        });
      }
      const figure = node('figure', null, 'health-svg-preview'); figure.append(previewImage);
      if (previewImage.dataset.renderFailed) {
        const warning = node('p', 'SVG 已生成，但浏览器无法显示此图。', 'health-notice');
        warning.id = 'health-svg-render-error'; figure.append(warning);
      }
      body.append(figure);
    } else {
      const empty = node('div', null, 'health-svg-placeholder');
      empty.append(icon(result.status === 'failed' ? 'image-off' : 'image'));
      empty.append(node('span', result.status === 'running' ? '模型正在生成图片…' : result.status === 'queued' ? '文字测速结束后生成' : '暂无图片'));
      body.append(empty);
    }
    const metrics = node('dl', null, 'health-svg-metrics');
    for (const [title, value] of [
      ['首字耗时', duration(result.metrics?.ttft_seconds)],
      ['总耗时', duration(result.duration_seconds)],
      ['端到端吞吐', result.status === 'success' ? speed(result.metrics?.end_to_end_tokens_per_second) : '—'],
      ['输出 token', Number.isFinite(result.metrics?.output_tokens) ? result.metrics.output_tokens : '未知'],
      ['图形', result.status === 'success' ? (result.animated ? '动画 SVG' : '静态 SVG') : '—']
    ]) { const metric = node('div'); metric.append(node('dt', title), node('dd', value)); metrics.append(metric); }
    body.append(metrics); section.append(body);
    if (result.sanitize_notes?.length) {
      const details = node('details', null, 'health-context'); details.dataset.healthDisclosure = 'svg-cleanup';
      details.append(node('summary', '安全清理记录'));
      result.sanitize_notes.forEach(text => details.append(node('p', text))); section.append(details);
    }
    return section;
  }
  /** Rebuild only changed reports, restoring disclosures, keyboard focus and scroll on polling. */
  function render(run) {
    const key = JSON.stringify(run), region = $('result');
    region.hidden = false;
    $('compare-current').disabled = false;
    if (key === renderedKey) { return; }
    const sameRun = renderedId === run.id, scrollY = window.scrollY;
    const focus = region.contains(document.activeElement) ? document.activeElement.closest('[data-health-disclosure]')?.dataset.healthDisclosure : null;
    if (renderedId) {
      disclosureStates.set(renderedId, new Map([...region.querySelectorAll('[data-health-disclosure]')].map(item => [item.dataset.healthDisclosure, item.open])));
      if (disclosureStates.size > 50) { disclosureStates.delete(disclosureStates.keys().next().value); }
    }
    summary(run);
    const checks = node('div', null, 'health-checks'), rows = run.doctor?.checks || [];
    const heading = node('h4', '本机检查', 'health-subheading'); heading.append(node('span', rows.length + ' 项')); checks.append(heading);
    for (const message of [run.error, run.model_blocked, run.doctor?.error,
      run.route_changed === true ? '检查期间代理配置发生变化，本轮结果不能归于单一路径。' : null,
      run.route_changed === null ? '未取得结束时的代理配置，无法确认路径是否变化。' : null]) {
      if (message) { checks.append(node('p', message, 'health-notice')); }
    }
    const groups = [
      ['network', '网络连接', 'network', row => row.id.startsWith('network.')],
      ['account', '账号与配置', 'key-round', row => ['config.load', 'auth.credentials'].includes(row.id)],
      ['local', '本机环境', 'monitor', row => ['installation', 'runtime.provenance', 'state.paths', 'system.disk'].includes(row.id)],
      ['desktop', '桌面客户端', 'app-window', row => row.id.startsWith('desktop.') || row.id === 'app_server.status']
    ];
    const grouped = new Set();
    groups.forEach(([id, title, symbol, match]) => {
      const selected = rows.filter(match); selected.forEach(row => grouped.add(row));
      if (selected.length) { checks.append(checkGroup(id, title, symbol, selected)); }
    });
    const other = rows.filter(row => !grouped.has(row));
    if (other.length) { checks.append(checkGroup('other', '其他检查', 'list-checks', other)); }
    if (!rows.length) { checks.append(node('p', isActive(run) ? '等待本机检查结果…' : '未取得本机检查结果', 'health-notice')); }
    // Backend advice sometimes covers model-level errors, not an individual doctor row.
    // Keep it accessible without repeating the same row advice in a second long list.
    const covered = new Set(rows.flatMap(row => [row.advice, row.title + '：' + row.advice])); covered.add(run.model_blocked);
    const extra = (run.overview?.advice || []).filter(text => !covered.has(text));
    if (extra.length) {
      const details = node('details', null, 'health-context'); details.dataset.healthDisclosure = 'advice'; details.append(node('summary', '补充建议'));
      extra.forEach(text => details.append(node('p', text))); checks.append(details);
    }
    const content = node('div', null, 'health-content'); content.append(svgResult(run), modelResults(run));
    for (const message of [run.error, run.model_blocked, run.route_changed ? '检查期间代理配置发生变化，本轮速度不能归于单一路径。' : null]) {
      if (message) { content.append(node('p', message, 'health-notice')); }
    }
    if (run.doctor || run.diagnostics !== false) {
      const diagnostics = node('details', null, 'health-diagnostics'); diagnostics.dataset.healthDisclosure = 'diagnostics';
      diagnostics.open = !run.model_check;
      const issues = rows.filter(isIssue).length;
      diagnostics.append(node('summary', '本机诊断' + (issues ? ' · ' + issues + ' 项需关注' : rows.length ? ' · ' + rows.length + ' 项' : '')), checks);
      content.append(diagnostics);
    }
    const saved = disclosureStates.get(run.id);
    content.querySelectorAll('[data-health-disclosure]').forEach(item => {
      if (saved?.has(item.dataset.healthDisclosure)) { item.open = saved.get(item.dataset.healthDisclosure); }
    });
    region.className = ''; region.replaceChildren(content); renderedId = run.id; renderedKey = key;
    if (sameRun) {
      const target = [...region.querySelectorAll('[data-health-disclosure]')].find(item => item.dataset.healthDisclosure === focus);
      target?.querySelector('summary')?.focus({ preventScroll: true });
      if (window.scrollY !== scrollY) { window.scrollTo({ top: scrollY, behavior: 'instant' }); }
    }
  }
  /** Resolve reports from the server, including completed records after a backend restart. */
  async function readRun(id) { return current?.id === id ? current : request('/run/' + encodeURIComponent(id)); }
  /** Ignore late historical reads after a user has selected a different record. */
  async function showReport() {
    const sequence = ++reportSequence, id = $('report').value;
    if (!id) {
      // Clearing a selection must not leave the previous report looking current.
      renderedKey = ''; $('summary').hidden = true; $('next-step').hidden = true;
      $('result').hidden = !!renderedId; $('compare-current').disabled = true; return;
    }
    try { const run = await readRun(id); if (sequence === reportSequence) { render(run); } }
    catch (e) { if (sequence === reportSequence) { error(e.message); } }
  }
  /** Compare evidence only, without claiming that a correlation establishes network causation. */
  async function compare() {
    const sequence = ++comparisonSequence, left = $('left').value, right = $('right').value;
    $('swap').disabled = !left || !right; $('differences').disabled = !left || !right;
    if (!left || !right) { comparisonKey = ''; $('comparison').replaceChildren(); $('compare-note').textContent = '尚未选择两条记录'; return; }
    try {
      const a = await readRun(left), b = await readRun(right);
      if (sequence !== comparisonSequence) { return; }
      const key = JSON.stringify([a, b, $('differences').checked]);
      if (comparisonKey === key) { return; }
      comparisonKey = key;
      const warnings = [];
      if (left === right) { warnings.push('当前选择的是同一条记录。'); }
      if ([a, b].some(run => run.status !== 'completed')) { warnings.push('至少一条记录未完成，速度仅代表已有成功样本。'); }
      if (['model', 'model_check', 'repeats', 'protocol', 'cli_version', 'sample_mode', 'diagnostics', 'svg_check'].some(key => a[key] !== b[key])) { warnings.push('模型、次数或检测方案不同，不是严格的同条件对比。'); }
      if (a.route_changed !== false || b.route_changed !== false) { warnings.push('至少一条记录无法确认全程代理配置不变。'); }
      $('compare-note').textContent = warnings.join(' ') + ' 账号身份、出口 IP 与 VPN 节点未校验；时间和上游负载也会影响结果。';
      const check = (run, id) => states[run.doctor?.checks?.find(row => row.id === id)?.status] || '未知';
      const samples = run => (run.samples || []).map(row => duration(row.metrics?.ttft_seconds)).join(' / ') || '未实测';
      const rows = [['环境', run => environments[run.environment]], ['时间', run => timestamp(run.started_at)],
        ['模型', run => run.model_check ? run.model : '未实测'], ['结论', run => run.verdict?.text], ['状态', run => states[run.status]],
        ['成功 / 已请求 / 计划', run => `${run.verdict?.success || 0} / ${attempts(run).length} / ${run.model_check ? run.repeats + Number(!!run.svg_check) : 0}`],
        ['出现容量不足', run => run.model_check && run.samples?.length && Number.isFinite(run.verdict?.capacity) ? run.verdict.capacity + ' 次请求' : '未知'],
        ['HTTP 可达性', run => check(run, 'network.provider_reachability')], ['WebSocket 握手', run => check(run, 'network.websocket_reachability')],
        ['首字耗时（中位数）', run => duration(median(measurements(run, 'ttft_seconds')))],
        ['输出吞吐（中位数）', run => speed(median(measurements(run, 'end_to_end_tokens_per_second')))],
        ['总耗时（中位数）', run => duration(median(measurements(run, 'duration')))],
        ['SVG 生成', run => run.svg_check ? states[run.svg_result?.status] || '未完成' : '未检测'],
        ['SVG 首字耗时', run => duration(run.svg_result?.metrics?.ttft_seconds)],
        ['SVG 总耗时', run => duration(run.svg_result?.duration_seconds)],
        ['SVG 端到端吞吐', run => run.svg_result?.status === 'success' ? speed(run.svg_result?.metrics?.end_to_end_tokens_per_second) : '—'],
        ['各次首字耗时', samples], ['账号来源', run => auth(run.auth_source)], ['CLI 版本', run => run.cli_version],
        ['本机 CLI 代理', run => route(run.route?.cli)], ['本站请求代理', run => route(run.route?.probe)]];
      const values = rows.map(([title, value]) => [title, value(a) ?? '未知', value(b) ?? '未知']);
      const filtered = $('differences').checked ? values.filter(row => row[1] !== row[2]) : values;
      // Describe both observations without interpreting a workplace label as the cause.
      const conclusion = node('p', 'A：' + (a.verdict?.text || '结论未知') + '；B：' + (b.verdict?.text || '结论未知'), 'health-compare-conclusion');
      $('comparison').replaceChildren(conclusion, filtered.length ? table(['检查项', '记录 A', '记录 B'], filtered) : node('p', '所选记录没有可见差异', 'health-empty'));
    } catch (e) { if (sequence === comparisonSequence) { error(e.message); } }
  }
  /** Preserve selection and focus while the unchanged historical list is polled. */
  function history(value) {
    const rows = value.runs, key = JSON.stringify(rows);
    records = rows;
    $('count').textContent = value.total + ' 条记录 · 最近 50 条';
    if (key === historyKey) { return; }
    historyKey = key;
    for (const name of ['report', 'left', 'right']) {
      const select = $(name), previous = select.value;
      select.replaceChildren(new Option('选择记录', ''), ...rows.map(run => new Option(label(run), run.id)));
      select.value = rows.some(row => row.id === previous) ? previous : name === 'report' ? (rows[0]?.id || '') : '';
    }
    // Prefer a company/home pair with matching test settings when history has both.
    // Do this only once: polling must never override the user's chosen comparison.
    if (!comparisonInitialized && rows.length > 1) {
      const company = rows.find(row => row.environment === 'company');
      const home = company && rows.find(row => row.environment === 'home' && ['model', 'model_check', 'repeats', 'protocol', 'sample_mode', 'diagnostics', 'svg_check'].every(key => row[key] === company[key]));
      $('left').value = home ? company.id : rows[0].id; $('right').value = home ? home.id : rows[1].id;
      comparisonInitialized = true;
    }
  }
  /** Disable only submission controls; viewing past reports remains available during a check. */
  function controls() {
    const busy = isActive(current);
    for (const name of ['environment', 'model-check', 'repeats', 'diagnostics', 'svg-check']) { $(name).disabled = submitting || busy; }
    // At least one check must remain selected; model-off explicitly becomes local-only.
    if (!$('model-check').checked) { $('diagnostics').checked = true; $('diagnostics').disabled = true; }
    $('model').disabled = submitting || busy || !$('model-check').checked;
    $('repeats').disabled ||= !$('model-check').checked;
    $('svg-check').disabled ||= !$('model-check').checked;
    $('start').disabled = submitting || busy || !catalog;
    $('stop').disabled = !busy || current.status === 'stopping';
    $('stop').hidden = !busy;
    $('cost').textContent = $('model-check').checked ? $('repeats').value + ' 次文字测速' + ($('svg-check').checked ? ' + 1 次 SVG 生成' : '') + ' · 消耗订阅额度' : '仅检查本机与连接，不发送模型输入';
  }
  /** Live progress describes the active job, never the selected historical result. */
  function progress() {
    $('live').dataset.state = current?.status || 'idle';
    $('live').hidden = !isActive(current);
    $('progress').textContent = !current ? '尚未开始新的体检' : isActive(current) ?
      current.phase + ' · ' + attempts(current).length + ' / ' + (current.model_check ? current.repeats + Number(!!current.svg_check) : 0) + ' 次模型请求' :
      '最近一次体检' + (states[current.status] || '状态未知');
    $('elapsed').textContent = current && Number.isFinite(current.started_at) ? Math.max(0, Math.round((current.finished_at || Date.now() / 1000) - current.started_at)) + ' 秒' : '';
  }
  /** Refresh read-only state with no overlapping polls and no scroll/focus reset. */
  async function refresh(models = false) {
    if (loading || submitting) { return; }
    const revision = mutation;
    loading = true; $('refresh').disabled = true; clearTimeout(timer);
    try {
      const state = await request(''), past = await request('/history');
      const nextCatalog = models || !catalog ? await request('/models') : catalog;
      // A POST can finish while this older GET is in flight. Never replace its
      // running state with a pre-submit snapshot or re-enable a second start.
      if (revision !== mutation) { return; }
      current = state.current; history(past);
      if (models || !catalog) {
        catalog = nextCatalog;
        const selected = $('model').value;
        $('model').replaceChildren(...(catalog.models || []).map(model => new Option(model, model)));
        if ([...$('model').options].some(option => option.value === selected)) { $('model').value = selected; }
        else if ((catalog.models || []).includes('gpt-6.1-sol')) { $('model').value = 'gpt-6.1-sol'; }
        $('account').textContent = auth(catalog.auth_source) + (catalog.ready ? '' : ' · 官方渠道未就绪');
      }
      progress(); await showReport();
      if (!$('compare-panel').hidden) { await compare(); }
      controls();
    } catch (e) { error(e.message); }
    finally { loading = false; $('refresh').disabled = false; if (visible) { timer = setTimeout(() => refresh(), 2500); } }
  }
  /** Reuse the same request ID after uncertain transport failures; a retry cannot spend twice. */
  async function start(event) {
    event.preventDefault(); if (submitting || $('start').disabled) { return; }
    const data = { environment: $('environment').value, model: $('model-check').checked ? $('model').value : '', repeats: Number($('repeats').value), model_check: $('model-check').checked, diagnostics: $('diagnostics').checked, svg_check: $('model-check').checked && $('svg-check').checked };
    const signature = JSON.stringify(data);
    if (!pending || pending.signature !== signature) { pending = { signature, request_id: crypto.randomUUID().replaceAll('-', '') }; }
    mutation++; reportSequence++; submitting = true; controls(); error(); $('progress').textContent = '正在提交检查…'; $('live').dataset.state = 'submitting'; $('live').hidden = false;
    try {
      current = await request('/start', { ...data, request_id: pending.request_id }); pending = null;
      historyKey = ''; history(await request('/history')); $('report').value = current.id;
      reportSequence++; render(current); progress(); window.BenchUI?.showPanel('health', 'check');
    } catch (e) { error(e.message + '。未确认启动时可再次点击，相同参数不会重复创建检查。'); }
    finally { submitting = false; controls(); }
  }
  /** Stop only the current ID so an old tab cannot cancel a different newly started check. */
  async function stop() {
    if (!current || $('stop').disabled) { return; }
    mutation++; $('stop').disabled = true; $('progress').textContent = '正在停止检查…';
    try { current = await request('/cancel', { run_id: current.id }); progress(); if ($('report').value === current.id) { render(current); } controls(); }
    catch (e) { error(e.message); controls(); }
  }
  $('form').addEventListener('submit', start);
  $('stop').addEventListener('click', stop);
  $('refresh').addEventListener('click', () => { error(); refresh(true); });
  $('model-check').addEventListener('change', controls);
  $('model').addEventListener('change', controls);
  $('repeats').addEventListener('change', controls);
  $('svg-check').addEventListener('change', controls);
  $('report').addEventListener('change', showReport);
  $('left').addEventListener('change', compare); $('right').addEventListener('change', compare);
  $('differences').addEventListener('change', compare);
  $('swap').addEventListener('click', () => {
    const left = $('left').value; $('left').value = $('right').value; $('right').value = left; compare();
  });
  $('compare-current').addEventListener('click', () => {
    const id = $('report').value; if (!id) { return; }
    $('left').value = id;
    if (!$('right').value || $('right').value === id) {
      const selected = records.find(row => row.id === id);
      const other = records.find(row => row.id !== id && row.environment !== selected?.environment && row.model === selected?.model) || records.find(row => row.id !== id);
      $('right').value = other?.id || '';
    }
    window.BenchUI?.showPanel('health', 'compare', true); compare();
  });
  $('compare-tab').addEventListener('click', compare);
  // Shared tabs also support keyboard activation, which does not emit a click.
  document.querySelector('[data-panel-tabs="health"]').addEventListener('keydown', () => {
    if (!$('compare-panel').hidden) { compare(); }
  });
  document.addEventListener('view-change', event => {
    visible = event.detail === 'health'; clearTimeout(timer);
    if (visible) { refresh(true); }
  });
}());
