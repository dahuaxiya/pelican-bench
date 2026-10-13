/** 持久化成绩浏览：只读历史接口，切换和筛选不会再次调用模型。 */
(function () {
  'use strict';
  const host = document.getElementById('performance-library');
  const tabs = document.getElementById('performance-tabs');
  const probe = document.getElementById('performance-probe');
  if (!host || !tabs || !probe) { return; }
  let view = 'compare', visible = false, page = 1, sequence = 0, timer;
  let data = null, loading = false, error = '';
  const filters = { site: '', model: '', concurrency: '', sort: 'speed' };
  const details = new Map();
  const states = { running: '运行中', stopping: '停止中', completed: '已结束', cancelled: '已停止',
    success: '成功', failed: '失败', queued: '排队中' };

  /** 创建纯文本节点；站点、模型名和上游诊断都不能作为 HTML 解析。 */
  function el(tag, text, cls) {
    const item = document.createElement(tag);
    if (text != null) { item.textContent = String(text); }
    if (cls) { item.className = cls; }
    return item;
  }

  /** 格式化可空指标，未观测值不能显示成零。
   * @param {number} value 原始观测值。
   * @param {string} unit 单位。
   * @return {string} 两位小数或未知值占位符。
   */
  function metric(value, unit = '') { return Number.isFinite(value) ? value.toFixed(2) + unit : '—'; }

  /** 展示本地时间，旧版没有记录时间时保持未知。 */
  function date(value) { return value ? new Date(value * 1000).toLocaleString() : '未记录'; }

  /** 创建命令按钮，统一用原生点击和焦点行为。 */
  function button(title, action, cls) {
    const item = el('button', title, cls); item.type = 'button'; item.addEventListener('click', action); return item;
  }

  /** 加载指定只读接口；HTTP 错误不当成空历史。
   * @param {string} url 固定历史接口及已编码参数。
   * @return {Promise<object>} 后端 JSON；失败交给调用者显示重试入口。
   */
  async function read(url) {
    const response = await fetch(url, { cache: 'no-store' });
    const value = await response.json();
    if (!response.ok || value.error) { throw new Error(value.error || '历史记录加载失败'); }
    return value;
  }

  /** 切换视图不重建测试表单；旧的网络响应通过序号失效，避免覆盖新筛选。 */
  function selectView(next) {
    view = next; page = 1; data = null; error = ''; sequence++; clearTimeout(timer);
    probe.hidden = view !== 'test'; host.hidden = view === 'test';
    probe.setAttribute('role', 'tabpanel'); host.setAttribute('role', 'tabpanel');
    probe.setAttribute('aria-labelledby', 'performance-tab-test');
    host.setAttribute('aria-labelledby', 'performance-tab-' + view);
    tabs.querySelectorAll('button').forEach(item => {
      const chosen = item.dataset.view === view;
      item.setAttribute('aria-selected', String(chosen)); item.tabIndex = chosen ? 0 : -1;
    });
    document.dispatchEvent(new CustomEvent('performance-panel-change', { detail: view }));
    if (view !== 'test' && visible) { load(); }
  }

  /** 在首次加载和运行批次结束时获取分页数据；串行轮询，离开页面即停。 */
  async function load() {
    if (!visible || view === 'test') { return; }
    clearTimeout(timer);
    const version = ++sequence, requestedView = view;
    loading = true; error = ''; render();
    try {
      const query = new URLSearchParams({ ...filters, page: String(page), page_size: '20' });
      const result = await read('/api/probe/' + requestedView + '?' + query);
      if (version !== sequence) { return; }
      data = result; page = data.page;
    } catch (e) {
      if (version === sequence) { error = e.message; }
    } finally {
      if (version === sequence) {
        loading = false; render();
        if (visible && view !== 'test' && data?.active && !error) { timer = setTimeout(load, 3000); }
      }
    }
  }

  /** 构造带可访问标签的筛选器，更换范围后从第一页开始。 */
  function selector(name, title, options) {
    const label = el('label', title), select = el('select'); select.setAttribute('aria-label', title);
    for (const [value, text] of options) {
      const option = el('option', text); option.value = value; select.append(option);
    }
    select.value = filters[name];
    select.addEventListener('change', () => { filters[name] = select.value; page = 1; load(); });
    label.append(select); return label;
  }

  /** 选择一条成绩的历史；站点与模型共同限定，避免把同名模型混成一个来源。 */
  function showHistory(row) {
    filters.site = row.site; filters.model = row.model; selectView('history');
  }

  /** 绘制跨批次最新有效成绩；条形比例按整个筛选结果计算，翻页不改变刻度。 */
  function comparison() {
    const wrap = el('div', null, 'performance-table-wrap'), table = el('table', null, 'performance-table');
    const head = el('thead'), headings = el('tr');
    for (const title of ['排名 / 模型', '站点', '端到端吞吐', '首字延迟', '并发上限', '测试时间', '历史']) {
      const cell = el('th', title); cell.scope = 'col'; headings.append(cell);
    }
    head.append(headings); table.append(head);
    const body = el('tbody');
    data.items.forEach((row, index) => {
      const tr = el('tr'), identity = el('td');
      identity.append(el('span', (page - 1) * data.page_size + index + 1, 'performance-rank'), el('strong', row.model));
      const speed = el('td'), value = row.metrics.end_to_end_tokens_per_second;
      speed.append(el('strong', metric(value, ' tokens/s')));
      const bar = el('span', null, 'capacity-bar'), fill = el('span');
      fill.style.width = Math.min(100, value / (data.max_speed || value) * 100) + '%'; bar.append(fill); speed.append(bar);
      const when = el('td', date(row.tested_at), 'performance-time');
      if (row.latest_status !== 'success') { when.append(el('small', '最近一次' + (states[row.latest_status] || row.latest_status))); }
      if (row.capacity_detected) { when.append(el('small', '本次曾出现容量不足')); }
      const history = el('td'); history.append(button(row.attempts + ' 次', () => showHistory(row), 'performance-history-link'));
      tr.append(identity, el('td', row.site), speed, el('td', metric(row.metrics.ttft_seconds, 's')),
        el('td', row.concurrency ?? '未记录'), when, history); body.append(tr);
    });
    table.append(body); wrap.append(table); return wrap;
  }

  /** 展开单批次完整记录；失败和无用量结果也显示，不能在历史中消失。 */
  async function runDetails(run, panel) {
    panel.textContent = '正在读取…';
    try {
      const value = details.get(run.id) || await read('/api/probe/history/' + encodeURIComponent(run.id));
      if (['completed', 'cancelled'].includes(value.status)) { details.set(run.id, value); }
      panel.replaceChildren();
      panel.append(el('p', '批次 ' + run.id + ' · 并发 ' + (run.concurrency ?? '未记录')
        + ' · 单项时限 ' + (run.timeout_seconds ?? '未记录') + ' 秒', 'capacity-note'));
      const wrap = el('div', null, 'performance-table-wrap'), table = el('table', null, 'performance-table');
      const headings = el('tr');
      for (const title of ['模型 / 站点', '状态', '吞吐', '首字延迟', '总耗时', '输出 token']) { headings.append(el('th', title)); }
      const thead = el('thead'); thead.append(headings); table.append(thead);
      const body = el('tbody');
      for (const row of value.rows) {
        const metrics = row.metrics || {}, tr = el('tr'), who = el('td');
        who.append(el('strong', row.model), el('small', row.site));
        const status = el('td', states[row.status] || row.status); status.dataset.status = row.status;
        // 上游重试成功也不能抹去已观察到的容量信号，历史与运行页保持相同口径。
        if (row.capacity_detected) { status.append(el('small', row.status === 'success' ? '容量不足 · 已恢复' : '曾出现容量不足')); }
        if (row.reason) { status.append(el('small', row.reason)); }
        tr.append(who, status, el('td', metric(metrics.end_to_end_tokens_per_second, ' tokens/s')),
          el('td', metric(metrics.ttft_seconds, 's')), el('td', metric(row.duration_seconds, 's')),
          el('td', metrics.output_tokens ?? '—')); body.append(tr);
      }
      table.append(body); wrap.append(table); panel.append(wrap);
      const link = el('a', '下载本轮 JSON'); link.href = '/api/probe/history/' + encodeURIComponent(run.id);
      link.download = run.id + '.json'; panel.append(link);
    } catch (e) {
      panel.replaceChildren(el('p', e.message), button('重试读取', () => runDetails(run, panel)));
    }
  }

  /** 历史批次按时间倒序展示，展开状态在自动刷新时保留。 */
  function history(expanded) {
    const list = el('div', null, 'performance-history');
    for (const run of data.items) {
      const item = el('details', null, 'performance-run'); item.dataset.id = run.id;
      const summary = el('summary'), title = el('div');
      title.append(el('strong', date(run.started_at)), el('small', run.sites.join(' · ')));
      summary.append(title, el('span', run.mode === 'performance' ? '输出测速' : '容量检测'),
        el('span', states[run.status] || run.status), el('span', run.success + '/' + run.total + ' 成功'),
        el('span', '并发 ' + (run.concurrency ?? '未记录')));
      const chevron = window.BenchUI?.icon('chevron-down');
      if (chevron) { summary.append(chevron); }
      const panel = el('div', null, 'performance-run-detail');
      item.append(summary, panel); item.open = expanded.has(run.id);
      item.addEventListener('toggle', () => { if (item.open && !panel.childNodes.length) { runDetails(run, panel); } });
      if (item.open) { runDetails(run, panel); }
      list.append(item);
    }
    return list;
  }

  /** 重绘只读展示区；正在测试的表单不受历史筛选或翻页影响。 */
  function render() {
    const expanded = new Set([...host.querySelectorAll('details[open]')].map(item => item.dataset.id));
    host.replaceChildren();
    const toolbar = el('div', null, 'performance-filters'), facets = data?.facets || {};
    toolbar.append(selector('site', '成绩站点', [['', '全部站点'], ...(facets.sites || [filters.site]).filter(Boolean).map(v => [v, v])]),
      selector('model', '成绩模型', [['', '全部模型'], ...(facets.models || [filters.model]).filter(Boolean).map(v => [v, v])]),
      selector('concurrency', '并发条件', [['', '全部并发'], ...(facets.concurrencies || [filters.concurrency]).filter(Boolean).map(v => [String(v), String(v)])]));
    if (view === 'compare') {
      toolbar.append(selector('sort', '成绩排序', [['speed', '吞吐从高到低'], ['latency', '首字从快到慢'], ['recent', '最近测试']]));
    }
    const refresh = button(loading ? '正在读取…' : '刷新成绩', load, 'performance-refresh'); refresh.disabled = loading;
    const icon = window.BenchUI?.icon('refresh-cw');
    if (icon) { refresh.replaceChildren(icon); refresh.setAttribute('aria-label', '刷新成绩'); refresh.title = '刷新成绩'; }
    toolbar.append(refresh); host.append(toolbar);
    const status = el('p', error || (data ? (view === 'compare' ? '最新有效成绩 · ' : '历史批次 · ')
      + data.total + ' 项' + (data.active ? ' · 有测试正在运行' : '') : '正在读取历史记录…'), 'performance-library-status');
    status.setAttribute('role', error ? 'alert' : 'status'); host.append(status);
    if (data?.storage_error) { const warning = el('p', data.storage_error); warning.setAttribute('role', 'alert'); host.append(warning); }
    if (data?.items?.length) { host.append(view === 'compare' ? comparison() : history(expanded)); }
    else if (!loading && !error) { host.append(el('p', view === 'compare' ? '暂无符合条件的有效测速成绩' : '暂无符合条件的测试记录', 'performance-empty')); }
    if (data?.total) {
      const pages = Math.ceil(data.total / data.page_size), pagination = el('nav', null, 'performance-pagination');
      pagination.setAttribute('aria-label', '成绩分页');
      const previous = button('上一页', () => { page--; load(); }), next = button('下一页', () => { page++; load(); });
      previous.disabled = loading || page <= 1; next.disabled = loading || page >= pages;
      pagination.append(previous, el('span', page + ' / ' + pages), next); host.append(pagination);
    }
  }

  for (const [name, title] of [['compare', '成绩对比'], ['test', '运行测试'], ['history', '历史记录']]) {
    const tab = button(title, () => selectView(name)); tab.dataset.view = name; tab.setAttribute('role', 'tab');
    tab.id = 'performance-tab-' + name; tab.setAttribute('aria-controls', name === 'test' ? probe.id : host.id); tabs.append(tab);
  }
  tabs.addEventListener('keydown', event => {
    const buttons = [...tabs.querySelectorAll('button')], current = buttons.indexOf(document.activeElement);
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { return; }
    event.preventDefault();
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
      : (current + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
    buttons[index].focus(); buttons[index].click();
  });
  tabs.hidden = false; selectView('compare');
  document.addEventListener('view-change', event => {
    visible = event.detail === 'performance'; clearTimeout(timer);
    if (visible && view !== 'test') { load(); }
    else if (!visible) { sequence++; }
  });
  document.addEventListener('performance-result', () => {
    if (visible && view !== 'test') { load(); }
  });
})();
