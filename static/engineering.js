/** 工程榜只展示宿主验收结果；模型输出一律作为文本，不能执行。 */
(function () {
  'use strict';
  const state = { ready: false, sites: [], models: {}, picked: new Set(), board: null, page: 1, size: 10,
    run: null, active: null, submitting: false, pending: null, timer: null, request: 0, loading: false, unsupported: {} };
  const modes = { default: '未指定推理参数', disabled: '请求关闭推理', enabled: '请求开启推理' };
  const statuses = { running: '运行中', stopping: '正在停止并回收容器', completed: '已结束', cancelled: '已停止',
    interrupted: '已中断', failed: '运行失败', resolved: '修复成功', unresolved: '未修复',
    service_error: '接口失败', output_limit: '输出截断', environment_error: '环境失败', runner_error: '执行器异常' };

  /** 展示准确的逐题状态；旧版本把输出截断记录为 service_error，仅修正其显示文案。
   * @param {Object} row 已持久化的逐题结果，不改写历史状态或分数
   * @return {string} 中文状态
   */
  function resultStatus(row) {
    if (row.status === 'service_error' && row.reason === 'agent: RuntimeError: 模型流未完整结束，未执行本次命令；finish_reason=length') {
      return statuses.output_limit;
    }
    return statuses[row.status] || row.status;
  }

  /** @param {string} id 固定控件 ID @return {HTMLElement} 页面节点 */
  function node(id) { return document.getElementById('swe-' + id); }

  /** @param {string} tag 标签 @param {string} text 文本 @return {HTMLElement} 安全文本节点 */
  function el(tag, text = '') { const item = document.createElement(tag); item.textContent = text; return item; }

  /** @param {string} path 同源 API @param {Object} body 可选请求体 @return {Promise<Object>} 响应数据 */
  async function api(path, body) {
    const response = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined);
    const data = await response.json();
    if (!response.ok) { throw new Error(data.error || 'HTTP ' + response.status); }
    return data;
  }

  /** @param {string} message 错误文本，空串清除 */
  function error(message = '') { node('error').textContent = message; node('error').hidden = !message; }

  /** @param {HTMLElement} select 选择器 @param {Array} items 值/文本对 @param {string} selected 当前选择 */
  function options(select, items, selected) {
    select.replaceChildren();
    (items.length ? items : [['', '暂无记录']]).forEach(([value, text]) => {
      const item = el('option', text); item.value = value; select.append(item);
    });
    if (items.some(item => item[0] === selected)) { select.value = selected; }
    select.disabled = !items.length;
  }

  /** @param {Array<string>} headings 表头 @param {Array<Array>} rows 文本或安全节点 @return {HTMLTableElement} 数据表 */
  function table(headings, rows) {
    const result = el('table'), head = el('thead'), header = el('tr'), body = el('tbody');
    headings.forEach(title => { const cell = el('th', title); cell.scope = 'col'; header.append(cell); });
    head.append(header);
    rows.forEach(values => {
      const row = el('tr'); values.forEach(value => { const cell = el('td');
        if (value instanceof HTMLElement) { cell.append(value); } else { cell.textContent = String(value); }
        row.append(cell);
      }); body.append(row);
    }); result.append(head, body); return result;
  }

  /** 整个筛选范围先计算并列排名再分页，最新有效成绩由服务器决定。 */
  function renderBoard() {
    const data = state.board;
    if (!data) { return; }
    const mode = node('scope').value;
    node('filter-wrap').hidden = mode === 'all';
    if (mode !== 'all') {
      const values = Array.from(new Set(data.entries.map(row => row[mode]))).sort();
      options(node('filter'), values.map(value => [value, value]), node('filter').value);
    }
    const rows = data.entries.filter(row => mode === 'all' || row[mode] === node('filter').value);
    node('metric-count').textContent = rows.length;
    node('metric-score').textContent = rows.length ? Math.max(...rows.map(row => row.success_rate)).toFixed(2) + '%' : '—';
    const group = data.cohorts.find(item => item.id === data.cohort_id);
    node('metric-suite').textContent = group ? group.conditions.suite.count + ' 题' : '—';
    const pages = Math.max(1, Math.ceil(rows.length / state.size));
    state.page = Math.min(pages, Math.max(1, state.page));
    const offset = (state.page - 1) * state.size;
    let previous = null, rank = 0;
    const ranked = rows.map((row, index) => {
      if (row.success_rate !== previous) { rank = index + 1; }
      previous = row.success_rate;
      const button = el('button'); button.type = 'button'; button.className = 'record-action';
      button.title = '查看记录'; button.setAttribute('aria-label', '查看 ' + row.site + ' / ' + row.model + ' 的记录');
      button.append(el('span', '查看记录'));
      const arrow = window.BenchUI?.icon('arrow-right'); if (arrow) { button.append(arrow); }
      button.addEventListener('click', () => showRun(row.run_id).catch(e => error(e.message)));
      return [rank, row.site, row.model, row.success_rate.toFixed(2) + '%', row.resolved + ' / ' + row.total,
        new Date(row.started_at * 1000).toLocaleString(), button];
    });
    node('board').replaceChildren(rows.length ? table(['排名', '站点', '模型', '修复成功率', '成功 / 总题数', '测试时间', '记录'], ranked.slice(offset, offset + state.size)) :
      (window.BenchUI?.emptyState('暂无有效的工程成绩', '完成一轮 SWE-bench 验证后，按官方测试结果计算修复成功率。', 'swe') || el('p', '暂无完整有效的工程成绩')));
    // Compact layouts place the site under the model so the main score stays visible.
    node('board').querySelectorAll('tbody tr').forEach((row, index) => {
      const entry = rows[offset + index];
      row.children[2].dataset.site = entry.site;
      row.children[2].setAttribute('aria-label', entry.model + '，站点 ' + entry.site);
    });
    node('pagination').hidden = !rows.length;
    node('range').textContent = '共 ' + rows.length + ' 条 · ' + (offset + 1) + '–' + Math.min(offset + state.size, rows.length);
    node('page-number').textContent = state.page + ' / ' + pages;
    node('prev').disabled = state.page === 1; node('next').disabled = state.page === pages;
  }

  /** 当前选择决定调用上界；没有就绪环境时，前端不允许启动。 */
  function plan() {
    const count = state.picked.size, questions = node('suite').value === 'lite' ? 300 : 3;
    const tasks = count * questions * Number(node('repeats').value);
    const apiCount = [...state.picked].filter(key => {
      const [siteName] = JSON.parse(key);
      return !state.sites.find(site => site.name === siteName)?.builtin;
    }).length;
    const attempts = questions * Number(node('repeats').value) * Number(node('steps').value) *
      (count + apiCount * Number(node('request-retries').value));
    node('plan').textContent = count ? tasks + ' 次任务 · 最多 ' + attempts + ' 次模型请求（含重试）' : '尚未选择模型';
    if (state.active) { node('plan').textContent = '有评测正在运行'; }
    else if (state.loading) { node('plan').textContent = '正在读取模型列表…'; }
    node('start').disabled = !state.ready || !count || !!state.active || state.submitting || state.loading;
    node('fields').disabled = !!state.active || state.submitting || state.loading;
  }

  /** 重绘保存的模型列表，刷新列表不会擅自选择新增模型。 */
  function renderModels() {
    node('models').replaceChildren();
    const available = new Set();
    state.sites.forEach(site => {
      const section = el('section'), title = el('strong', site.name), choices = el('div'); choices.className = 'swe-choices';
      section.dataset.site = site.name.toLowerCase();
      const head = el('div'); head.className = 'iq-site-head';
      const all = el('button', '全选本站'), none = el('button', '清空');
      all.type = none.type = 'button';
      all.setAttribute('aria-label', '全选 ' + site.name + ' 的模型');
      none.setAttribute('aria-label', '清空 ' + site.name + ' 的模型');
      all.addEventListener('click', () => selectAll(true, choices));
      none.addEventListener('click', () => selectAll(false, choices));
      head.append(title, all, none); section.append(head, choices);
      if (site.note || site.error) { section.append(el('p', (site.error || site.note) + ' · 文本命令，容器执行')); }
      (state.models[site.name] || []).forEach(model => {
        const reason = Object.entries(state.unsupported).find(([prefix]) => model.toLowerCase().startsWith(prefix))?.[1];
        const key = JSON.stringify([site.name, model]);
        if (!reason) { available.add(key); }
        const label = el('label'), input = el('input'); input.type = 'checkbox'; input.dataset.key = key;
        label.dataset.search = (site.name + ' ' + model).toLowerCase();
        input.disabled = !!reason;
        input.checked = !reason && state.picked.has(key);
        if (reason) { label.title = reason; }
        input.addEventListener('change', () => { if (input.checked) { state.picked.add(key); } else { state.picked.delete(key); } plan(); });
        label.append(input, el('span', model + (reason ? '（不支持工程评测）' : ''))); choices.append(label);
      });
      all.disabled = none.disabled = !choices.querySelector('input:not([disabled])');
      if (!choices.children.length) { const empty = el('p', '暂无模型'); empty.className = 'muted'; choices.append(empty); }
      node('models').append(section);
    });
    state.picked.forEach(key => { if (!available.has(key)) { state.picked.delete(key); } });
    filterModels(); plan();
  }

  /** Filter without changing selection; hidden choices remain part of the request budget. */
  function filterModels() {
    const query = node('model-search').value.trim().toLowerCase();
    let count = 0;
    node('models').querySelectorAll('section').forEach(section => {
      let visible = 0;
      section.querySelectorAll('label').forEach(label => {
        label.hidden = !label.dataset.search.includes(query);
        if (!label.hidden) { visible += 1; }
      });
      section.hidden = !!query && !visible;
      count += visible;
    });
    node('model-empty').hidden = count > 0 || !query;
  }

  /**
   * Select candidates without starting a task; search never silently changes the global meaning of all.
   * @param {boolean} picked Select all or clear all.
   * @param {HTMLElement} scope All model choices or a single site's choices.
   */
  function selectAll(picked, scope = node('models')) {
    if (node('fields').disabled) { return; }
    scope.querySelectorAll('input[type=checkbox]:not([disabled])').forEach(input => {
      input.checked = picked;
      if (picked) { state.picked.add(input.dataset.key); } else { state.picked.delete(input.dataset.key); }
    }); plan();
  }

  /** 逐站点刷新模型；单站失败保留旧候选，不阻止其他站点加载，也不自动勾选新模型。 */
  async function loadModels() {
    state.loading = true; plan(); error();
    const failures = [];
    try {
      for (const site of state.sites) {
        try {
          const result = await api('/api/models?site=' + encodeURIComponent(site.name) + '&refresh=1');
          if (result.error) { throw new Error(result.error); }
          state.models[site.name] = result.models;
        } catch (e) {
          // 站点互不依赖；失败时不把旧列表清空，否则会丢掉用户已经勾选的模型。
          failures.push(site.name + '：' + e.message);
        }
      }
      renderModels();
      error(failures.join('；'));
    } catch (e) { error(e.message); }
    finally { state.loading = false; plan(); }
  }

  /** 展示批次条件、逐题证据及物理请求时间；缺失的单题时限标为未记录，不套用表单当前值。
   * @param {Object} run 包含逐题结果的批次快照；源码、日志均通过 textContent 展示
   */
  function renderRun(run) {
    state.run = run;
    node('export').disabled = !run;
    node('stop').hidden = !state.active;
    node('stop').textContent = run.managed_by_url ? '前往原服务管理任务' : '停止';
    if (!run) { return; }
    node('status').textContent = (statuses[run.status] || run.status) + ' · ' + run.done + '/' + run.total + ' · ' + run.phase + (run.error ? ' · ' + run.error : '');
    node('status').textContent += ' · 单步最多重试 ' + (run.options?.request_retries ?? 0) + ' 次';
    node('status').textContent += ' · 单题时限 ' + (run.options?.task_timeout == null ? '未记录' : run.options.task_timeout + ' 秒');
    node('status').textContent += ' · 请求超时 ' + (run.options?.request_timeout ?? 180) + ' 秒';
    node('status').textContent += run.options?.stream ? ' · 流式' : ' · 非流式';
    node('status').textContent += run.options?.action_mode === 'tool_calls' ? ' · 工具调用' : ' · 文本命令';
    node('status').textContent += run.options?.output_limit_policy === 'agent_feedback' ? ' · 截断后提示精简' : ' · 截断即停止';
    if (run.configurations?.some(c => c.execution && c.execution.transport !== 'kimi-acp')) {
      node('status').textContent += ' · Codex 使用 medium / 文本命令 / 无重试 / CLI 输出预算';
    }
    if (run.configurations?.some(c => c.execution?.transport === 'kimi-acp')) {
      node('status').textContent += ' · Kimi 使用默认推理 / 文本命令 / 无重试 / CLI 输出预算';
    }
    if (run.progress && ['running', 'stopping'].includes(run.status)) {
      const now = Date.now() / 1000;
      const request = run.progress.request;
      node('status').textContent += ' · ' + run.progress.detail;
      if (request) {
        // 收流进度会定期更新；总用时必须从请求开始计算，不能每次更新都归零。
        node('status').textContent += ' · 本次请求已用时 ' + Math.max(0, Math.floor(now - request.started_at)) + ' 秒';
        if (request.received_at != null) {
          node('status').textContent += ' · ' + Math.max(0, Math.floor(now - request.received_at)) + ' 秒前收到数据';
        }
      } else {
        node('status').textContent += ' · 当前状态已持续 ' + Math.max(0, Math.floor(now - run.progress.updated_at)) + ' 秒';
      }
    }
    node('progress').hidden = false; node('progress').max = run.total; node('progress').value = run.done;
    node('summary').replaceChildren(table(['站点', '模型', '进度', '修复成功率'], run.summary.map(row =>
      [row.site, row.model, row.completed + '/' + row.total, row.success_rate == null ? '未形成有效成绩' : row.success_rate.toFixed(2) + '%'])));
    // Polling must not close the patch/test log the user is currently reading.
    const opened = new Set(Array.from(node('results').querySelectorAll('details[open]'), item => item.dataset.resultKey));
    node('results').replaceChildren();
    run.results.forEach(row => {
      const detail = el('details'), summary = el('summary', row.site + ' / ' + row.model + ' · ' + row.instance_id + ' · 第 ' + row.repeat + ' 次 · ' + resultStatus(row));
      detail.dataset.resultKey = JSON.stringify([row.site, row.model, row.instance_id, row.repeat]);
      detail.open = opened.has(detail.dataset.resultKey);
      detail.append(summary);
      if (row.source_run_id) { detail.append(el('p', '沿用相同条件下的已完成任务，来源批次：' + row.source_run_id)); }
      if (row.reason) { detail.append(el('p', row.reason)); }
      if (row.exit_status) { detail.append(el('p', '退出原因：' + row.exit_status)); }
      if (row.model_calls != null) { detail.append(el('p', '模型步骤 ' + row.model_calls + ' 次 · 实际请求 ' + (row.request_attempts ?? row.model_calls) + ' 次')); }
      [['补丁', row.patch], ['请求重试记录', row.retry_events?.length ? JSON.stringify(row.retry_events, null, 2) : ''],
        ['镜像准备日志', row.image_pull_log],
        ['未执行的模型回复', row.response_diagnostics ? JSON.stringify(row.response_diagnostics, null, 2) : ''],
        ['官方测试结果', row.tests_status ? JSON.stringify(row.tests_status, null, 2) : ''], ['测试日志', row.test_log],
        ['执行记录', row.trajectory ? JSON.stringify(row.trajectory, null, 2) : '']].forEach(([title, value]) => {
        if (value) { detail.append(el('h4', title), el('pre', value)); }
      });
      node('results').append(detail);
    });
  }

  /** @param {string} id 选择的历史 ID @return {Promise<void>} 切换批次，忽略之前轮询的迟到响应 */
  async function showRun(id) {
    const ticket = ++state.request;
    const run = await api('/api/swe/run/' + id + '?full=1');
    if (ticket !== state.request) { return; }
    node('history').value = id; renderRun(run);
    window.BenchUI?.showPanel('swe', 'records', true);
    // 历史切换会使在途刷新失效；若还有活跃批次，继续轮询它的状态。
    if (state.active) { clearTimeout(state.timer); state.timer = setTimeout(refresh, 5000); }
  }

  /** 刷新环境、榜单与运行记录；按保存的预算区分榜单，刷新失败时清空旧榜单。
   * @return {Promise<void>} 刷新结束；迟到响应不覆盖较新的视图
   */
  async function refresh() {
    clearTimeout(state.timer);
    const ticket = ++state.request;
    try {
      const environment = await api('/api/swe/environment');
      state.ready = environment.ready;
      const unsupported = environment.unsupported_model_prefixes || {};
      if (JSON.stringify(unsupported) !== JSON.stringify(state.unsupported)) {
        // 能力限制来自后端同一份规则；规则更新后清掉失效选项，避免全选发起无效付费请求。
        state.unsupported = unsupported; renderModels();
      }
      node('environment').textContent = environment.ready ? '环境就绪' : environment.issues.join('；');
      node('environment').dataset.ready = String(environment.ready);
      const board = await api('/api/swe/leaderboard' + (node('cohort').value ? '?cohort=' + encodeURIComponent(node('cohort').value) : ''));
      const history = await api('/api/swe/runs');
      if (ticket !== state.request) { return; }
      state.board = board;
      options(node('cohort'), board.cohorts.map(group => [group.id, group.conditions.suite.count + ' 题 · 每题 ' +
        group.conditions.options.repeats + ' 次 · ' + group.conditions.options.step_limit + ' 步 · ' +
        (group.conditions.execution ? (group.conditions.execution.transport === 'kimi-acp' ? 'Kimi' : 'Codex') + ' 登录态 · CLI 输出预算' : group.conditions.options.max_tokens + ' tokens') + ' · 单步重试 ' + (group.conditions.options.request_retries ?? 0) + ' 次 · 单题时限 ' +
        (group.conditions.options.task_timeout == null ? '未记录' : group.conditions.options.task_timeout + ' 秒') +
        ' · 请求超时 ' + (group.conditions.options.request_timeout ?? 180) +
        ' 秒 · ' + (group.conditions.options.stream ? '流式' : '非流式') + ' · ' +
        (group.conditions.options.action_mode === 'tool_calls' ? '工具调用' : '文本命令') + ' · ' +
        (group.conditions.options.output_limit_policy === 'agent_feedback' ? '截断后提示精简' : '截断即停止') + ' · ' +
        (group.conditions.execution ? group.conditions.execution.reasoning_effort + ' · ' + group.conditions.execution.cli_version : modes[group.conditions.thinking_mode]) + ' · ' + group.id.slice(0, 6)]), board.cohort_id);
      renderBoard();
      state.active = history.runs.find(run => ['running', 'stopping'].includes(run.status))?.run_id || null;
      const activeRun = history.runs.find(run => run.run_id === state.active);
      node('active-run').hidden = !activeRun;
      if (activeRun) { node('active-run').textContent = '工程评测运行中 · 查看当前进度'; }
      options(node('history'), history.runs.map(run => [run.run_id, new Date(run.started_at * 1000).toLocaleString() + ' · ' + (statuses[run.status] || run.status)]), state.run?.run_id || state.active);
      if (node('history').value) {
        const run = await api('/api/swe/run/' + node('history').value + '?full=1');
        if (ticket !== state.request) { return; }
        renderRun(run);
      }
      error();
    } catch (e) {
      // 旧筛选请求迟到失败，不能清空后来成功加载的新榜单。
      if (ticket !== state.request) { return; }
      state.board = null; node('board').replaceChildren(); node('pagination').hidden = true; error(e.message);
      ['count', 'score', 'suite'].forEach(name => { node('metric-' + name).textContent = '—'; });
    } finally {
      if (ticket === state.request) { plan(); if (state.active) { state.timer = setTimeout(refresh, 5000); } }
    }
  }

  /**
   * 提交包括请求超时的完整预算；同参数重试沿用 ID，参数改变才创建新计费任务。
   * @param {SubmitEvent} event 表单提交事件
   */
  async function start(event) {
    event.preventDefault();
    if (node('start').disabled || !node('form').reportValidity()) { return; }
    const body = { suite: node('suite').value, repeats: Number(node('repeats').value), step_limit: Number(node('steps').value),
      max_tokens: Number(node('tokens').value), task_timeout: Number(node('timeout').value),
      request_timeout: Number(node('request-timeout').value),
      request_retries: Number(node('request-retries').value),
      tasks: Array.from(state.picked).map(key => { const [site, model] = JSON.parse(key); return { site, model }; }) };
    const signature = JSON.stringify(body);
    if (state.pending?.signature !== signature) { state.pending = { signature, id: crypto.randomUUID().replaceAll('-', '') }; }
    body.request_id = state.pending.id;
    state.submitting = true; plan(); error();
    try {
      state.run = await api('/api/swe/run', body); state.active = state.run.run_id; state.pending = null;
      window.BenchUI?.showPanel('swe', 'records', true);
      await refresh();
    } catch (e) { error(e.message); }
    finally { state.submitting = false; plan(); }
  }

  /** 初始化只读取本机配置，不因页面打开而向模型发送请求。 */
  async function init() {
    try {
      const saved = await api('/api/config'), gallery = await api('/api/gallery');
      state.sites = saved.sites;
      state.sites.forEach(site => { state.models[site.name] = Array.from(new Set((site.models || []).concat((gallery.cells || []).filter(row => row.site === site.name).map(row => row.model)))).sort(); });
      renderModels(); await refresh();
    } catch (e) { error(e.message); }
  }

  node('form').addEventListener('submit', start);
  node('fields').addEventListener('change', plan);
  node('all').addEventListener('click', () => selectAll(true));
  node('none').addEventListener('click', () => selectAll(false));
  node('load').addEventListener('click', loadModels);
  node('model-search').addEventListener('input', filterModels);
  node('refresh').addEventListener('click', refresh);
  node('record-refresh').addEventListener('click', refresh);
  node('cohort').addEventListener('change', () => { state.page = 1; refresh(); });
  node('scope').addEventListener('change', () => { state.page = 1; renderBoard(); });
  node('filter').addEventListener('change', () => { state.page = 1; renderBoard(); });
  node('page-size').addEventListener('change', () => { state.size = Number(node('page-size').value); state.page = 1; renderBoard(); });
  node('prev').addEventListener('click', () => { state.page -= 1; renderBoard(); });
  node('next').addEventListener('click', () => { state.page += 1; renderBoard(); });
  node('history').addEventListener('change', () => showRun(node('history').value).catch(e => error(e.message)));
  node('active-run').addEventListener('click', () => {
    if (state.active) { showRun(state.active).catch(e => error(e.message)); }
  });
  node('stop').addEventListener('click', async () => {
    if (state.run?.managed_by_url) { window.location.href = state.run.managed_by_url; return; }
    if (!state.active) { return; }
    try { await api('/api/swe/cancel', { run_id: state.active }); await refresh(); } catch (e) { error(e.message); }
  });
  node('export').addEventListener('click', () => {
    if (!state.run) { return; }
    const url = URL.createObjectURL(new Blob([JSON.stringify(state.run, null, 2)], { type: 'application/json' }));
    const link = el('a'); link.href = url; link.download = 'swe-' + state.run.run_id + '.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  init();
}());
