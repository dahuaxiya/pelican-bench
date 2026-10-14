/** 站点性能测评；按各渠道的观测能力展示指标，不用估算用量冒充实测速度。 */
(function () {
  'use strict';
  let host, models = [], selected = '', timer, busy = false, mode = 'capacity';
  let state = { status: 'idle', rows: [] };
  let sites = [], siteSelected = '', catalogBusy = false, catalogError = '', catalogVersion = 0;
  let pageVisible = true, pageLoading = false;
  const targets = new Map();
  let selectionInitialized = false, batchLoading = false, selectionError = '';
  let modelFilter = '', resultSort = 'original';
  const concurrencyByMode = { performance: 32, capacity: 2 };
  const active = () => ['running', 'stopping'].includes(state.status);

  /** 使用站点和模型共同标识测试项，避免不同渠道的同名模型互相覆盖。
   * @param {string} site 已配置站点名。
   * @param {string} model 站点模型目录中的原名。
   * @return {string} 无分隔符碰撞的选择键，也用于保存结果展开状态。
   */
  function targetKey(site, model) { return JSON.stringify([site || '', model]); }

  /** 构造纯文本节点，模型名和错误信息都不作为 HTML 解释。 */
  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text) { el.textContent = text; }
    if (className) { el.className = className; }
    return el;
  }

  /** 使用工作区已有 Lucide 图标，图标按钮保留名称及原生悬浮提示。
   * @param {HTMLElement} button 已绑定行为的按钮。
   * @param {string} name 工作区支持的图标名称。
   * @param {boolean} only 是否只显示图标；无图标库时保留文字。
   */
  function decorateButton(button, name, only = false) {
    const symbol = window.BenchUI?.icon(name);
    button.setAttribute('aria-label', button.textContent); button.title = button.textContent;
    if (!symbol) { return; }
    if (only) { button.replaceChildren(symbol); button.classList.add('icon-button'); }
    else { button.prepend(symbol); }
  }

  /** 在当前目录内筛选名称，不更改隐藏模型的选择，不触发网络或打断输入焦点。
   * @param {HTMLElement} root 已挂载区域或尚未挂载的完整控件，用于在替换前完成筛选。
   */
  function filterModels(root = host) {
    const rows = [...root.querySelectorAll('.capacity-picker > .capacity-models .capacity-model')];
    rows.forEach(row => { row.hidden = !row.textContent.toLowerCase().includes(modelFilter.trim().toLowerCase()); });
    const empty = root.querySelector('.capacity-no-match');
    if (empty) { empty.hidden = rows.some(row => !row.hidden) || catalogBusy; }
  }

  /** 读取或更新当前检测，固定同源端点，不允许浏览器提供凭据和提示词。 */
  async function request(action, body) {
    const response = await fetch('/api/probe' + action, body === undefined
      ? { cache: 'no-store' }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok || data.error) { throw new Error(data.error || '检测请求失败'); }
    return data;
  }

  /** 按观测证据生成行状态；即使恢复成功也保留容量不足标记。 */
  function label(row) {
    if (row.capacity_detected) {
      return '发现容量不足 · ' + ({ success: '已恢复', failed: '请求失败', running: '等待恢复', cancelled: '已停止' }[row.status] || row.status);
    }
    return { queued: '排队中', running: '检测中', success: '请求成功 · 未见容量信号',
      failed: '请求失败 · 容量未确认', cancelled: '已停止 · 无结论' }[row.status] || row.status;
  }

  /** 绘制当前站点复选列表和跨站点已选清单；切换目录不会隐式勾选其他模型。 */
  function renderSelection() {
    const locked = busy || active() || catalogBusy || batchLoading;
    const picker = node('div', null, 'capacity-picker');
    const search = node('label', null, 'search-field capacity-search');
    const searchIcon = window.BenchUI?.icon('search'); if (searchIcon) { search.append(searchIcon); }
    const input = node('input'); input.type = 'search'; input.placeholder = '搜索模型'; input.value = modelFilter;
    input.setAttribute('aria-label', '搜索测试模型');
    input.addEventListener('input', () => { modelFilter = input.value; filterModels(); }); search.append(input);
    const toolbar = node('div', null, 'capacity-selection-toolbar');
    const more = node('details', null, 'capacity-more');
    const moreTitle = node('summary', '更多操作'); moreTitle.setAttribute('aria-label', '批量选择'); moreTitle.title = '批量选择';
    const moreIcon = window.BenchUI?.icon('ellipsis'); if (moreIcon) { moreTitle.replaceChildren(moreIcon); }
    const menu = node('div', null, 'capacity-menu');
    more.append(moreTitle, menu);
    more.addEventListener('keydown', event => {
      if (event.key === 'Escape') { more.open = false; moreTitle.focus(); }
    });
    for (const [title, apply] of [
      ['全选本站', () => models.forEach(model => targets.set(targetKey(siteSelected, model), { site: siteSelected, model }))],
      ['清空本站', () => [...targets].forEach(([key, target]) => { if (target.site === siteSelected) { targets.delete(key); } })],
      ['清空已选', () => targets.clear()]
    ]) {
      const button = node('button', title); button.type = 'button'; button.disabled = locked;
      button.addEventListener('click', () => { selectionInitialized = true; apply(); render(); });
      (title === '全选本站' ? toolbar : menu).append(button);
    }
    const all = node('button', batchLoading ? '正在加载全部站点…' : '全选所有站点');
    all.type = 'button'; all.disabled = locked;
    all.addEventListener('click', selectAllSites); menu.prepend(all); toolbar.append(node('span', models.length + ' 个模型', 'capacity-catalog-count'), more);
    picker.append(search, toolbar);
    const list = node('div', null, 'capacity-models');
    list.setAttribute('role', 'group'); list.setAttribute('aria-label', '选择测试模型');
    for (const model of models) {
      const label = node('label', null, 'capacity-model');
      const box = node('input'); box.type = 'checkbox'; box.disabled = locked;
      box.checked = targets.has(targetKey(siteSelected, model));
      box.addEventListener('change', () => {
        selectionInitialized = true;
        if (box.checked) { targets.set(targetKey(siteSelected, model), { site: siteSelected, model }); }
        else { targets.delete(targetKey(siteSelected, model)); }
        render();
      });
      label.append(box, node('span', model)); list.append(label);
    }
    const empty = node('p', '没有匹配的模型', 'capacity-no-match capacity-note'); empty.hidden = true; list.append(empty);
    picker.append(list);
    const chosen = node('details', null, 'capacity-selection');
    chosen.append(node('summary', '已选 ' + new Set([...targets.values()].map(target => target.site)).size
      + ' 个站点 · ' + targets.size + ' 个模型'));
    const selectedList = node('div', null, 'capacity-models');
    for (const [key, target] of targets) {
      const label = node('label', null, 'capacity-model');
      const box = node('input'); box.type = 'checkbox'; box.checked = true; box.disabled = locked;
      box.addEventListener('change', () => { targets.delete(key); selectionInitialized = true; render(); });
      label.append(box, node('span', target.site + ' / ' + target.model)); selectedList.append(label);
    }
    chosen.append(selectedList); picker.append(chosen);
    if (selectionError) { const error = node('p', selectionError); error.setAttribute('role', 'alert'); picker.append(error); }
    return picker;
  }

  /** 完整构建后一次替换检测区域，避免轮询期间的临时空页面改变滚动位置。
   * @param {string} [error] 已转换为可展示文本的接口错误，不作为 HTML 解析。
   */
  function render(error) {
    if (!host?.isConnected) { return; }
    const expanded = new Set([...host.querySelectorAll('.capacity-result[open]')].map(el => el.dataset.model));
    const selectionOpen = host.querySelector('.capacity-selection')?.open;
    // 轮询会重绘列表，保留滚动和键盘焦点，不能让正在选择模型的用户每秒回到开头。
    const listScroll = host.querySelector('.capacity-picker > .capacity-models')?.scrollTop || 0;
    const focusedModel = document.activeElement?.closest('.capacity-model')?.textContent;
    const searchFocused = document.activeElement?.getAttribute('aria-label') === '搜索测试模型';
    const searchPosition = searchFocused ? document.activeElement.selectionStart : null;
    const controls = node('section', null, 'capacity-setup'); controls.setAttribute('aria-label', '测试范围');
    const results = node('section', null, 'capacity-results'); results.setAttribute('aria-label', '测试结果');
    // 先在离线节点中构建两列。旧实现清空后设置 scrollTop 会触发布局，
    // 此时结果列还不存在，浏览器按临时变短的页面把用户滚动位置截到顶部。
    controls.append(node('h3', '测试范围'));
    const modes = node('div', null, 'capacity-modes');
    modes.setAttribute('role', 'group'); modes.setAttribute('aria-label', '检测模式');
    for (const [value, title] of [['performance', '输出测速'], ['capacity', '容量检测']]) {
      const option = node('button', title); option.type = 'button';
      option.setAttribute('aria-pressed', String(mode === value)); option.disabled = busy || active() || batchLoading;
      option.addEventListener('click', () => { mode = value; render(); }); modes.append(option);
    }
    const actions = node('div', null, 'capacity-actions');
    if (sites.length) {
      const siteSelect = node('select'); siteSelect.setAttribute('aria-label', '检测站点');
      for (const site of sites) {
        const option = node('option', site.name); option.value = site.name; siteSelect.append(option);
      }
      siteSelect.value = siteSelected; siteSelect.disabled = busy || active() || batchLoading;
      siteSelect.addEventListener('change', () => {
        siteSelected = siteSelect.value; selected = ''; modelFilter = ''; loadModels();
      });
      const siteLabel = node('label', '站点', 'capacity-site'); siteLabel.append(siteSelect); actions.append(siteLabel);
    }
    if (!sites.length) {
      const select = node('select'); select.setAttribute('aria-label', '检测模型');
      for (const name of ['all', ...models]) {
        const option = node('option', name === 'all' ? '该站点全部模型' : name);
        option.value = name; select.append(option);
      }
      select.value = selected; select.disabled = busy || active() || catalogBusy || !models.length;
      select.addEventListener('change', () => { selected = select.value; }); actions.append(select);
    }
    const run = node('button', busy ? '正在提交…' : active() ? '检测中…' : mode === 'performance' ? '开始测速' : '快速检测');
    run.type = 'button'; run.dataset.icon = 'activity';
    run.className = 'primary capacity-launch'; decorateButton(run, active() || busy ? 'loader-circle' : 'play');
    run.disabled = busy || active() || catalogBusy || batchLoading || (sites.length ? !targets.size : !models.length);
    run.addEventListener('click', () => {
      if (!concurrencyInput.reportValidity()) { return; }
      const concurrency = Number(concurrencyInput.value);
      update('', sites.length ? { targets: [...targets.values()], mode, concurrency }
        : { model: selected, mode, concurrency });
    });
    const runActions = node('div', null, 'capacity-actions capacity-launchbar'); runActions.append(run);
    if (sites.length) {
      const refresh = node('button', '刷新模型'); refresh.type = 'button'; refresh.dataset.icon = 'refresh-cw';
      refresh.disabled = busy || active() || catalogBusy || batchLoading;
      decorateButton(refresh, 'refresh-cw', true);
      refresh.addEventListener('click', () => loadModels(true)); actions.append(refresh);
    }
    if (active()) {
      const stop = node('button', state.status === 'stopping' ? '正在停止…' : '停止检测');
      stop.type = 'button'; stop.dataset.icon = 'square'; stop.disabled = busy || state.status === 'stopping';
      stop.className = 'danger'; decorateButton(stop, 'square');
      stop.addEventListener('click', () => update('/cancel', {})); runActions.append(stop);
    }
    controls.append(modes, actions);
    if (sites.length) {
      controls.append(renderSelection());
      filterModels(controls);
      controls.querySelector('.capacity-selection').open = Boolean(selectionOpen);
    }
    const concurrencyLabel = node('label', '并发数', 'capacity-concurrency');
    const concurrencyInput = node('input'); concurrencyInput.type = 'number';
    concurrencyInput.min = '1'; concurrencyInput.max = '200'; concurrencyInput.step = '1'; concurrencyInput.required = true;
    concurrencyInput.value = concurrencyByMode[mode]; concurrencyInput.setAttribute('aria-label', '测试并发数');
    concurrencyInput.disabled = busy || active();
    // 输入期间不重绘，允许暂时清空再输入；运行中使用服务端快照，避免显示错误预算。
    concurrencyInput.addEventListener('input', () => { concurrencyByMode[mode] = concurrencyInput.value; });
    concurrencyLabel.append(concurrencyInput); controls.append(concurrencyLabel, runActions);
    controls.append(node('p', mode === 'performance' ? '单项 180 秒 · 每模型 1 次' : '单项 45 秒 · 每模型 1 次', 'capacity-budget'));
    if (catalogBusy) { controls.append(node('p', '正在加载站点模型…', 'capacity-note')); }
    if (batchLoading) { controls.append(node('p', '正在加载全部站点…', 'capacity-note')); }
    if (catalogError) {
      const notice = node('p', catalogError); notice.setAttribute('role', 'alert'); controls.append(notice);
      const settings = node('a', '管理站点与账号'); settings.href = '#settings'; controls.append(settings);
      if (!sites.length) {
        const retry = node('button', '重新加载站点'); retry.type = 'button'; retry.dataset.icon = 'refresh-cw';
        retry.disabled = pageLoading; retry.addEventListener('click', openPage); controls.append(retry);
      }
    }
    const count = state.rows.filter(row => row.capacity_detected).length;
    const done = state.rows.filter(row => !['queued', 'running'].includes(row.status)).length;
    const failed = state.rows.some(row => ['failed', 'cancelled'].includes(row.status));
    const performanceRun = state.mode === 'performance';
    let text = mode === 'performance' ? '每模型 1 次长输出 · 600–800 英文词 · 每条最多 180 秒'
      : '每模型 1 条短请求 · 每条最多 45 秒';
    if (state.status !== 'idle') {
      if (performanceRun) {
        text = active() ? '测速进行中' : state.status === 'cancelled' ? '测速已停止'
          : failed ? '测速结束，部分请求未成功' : '测速完成';
        if (count) { text += ' · 发现容量不足：' + count + ' 个模型'; }
      } else {
        text = count ? '本轮发现容量不足：' + count + ' 个模型' : active() ? '检测进行中'
          : failed ? '未取得明确容量证据，部分请求未成功' : '本轮短请求未观察到容量不足';
      }
      text += ' · ' + done + '/' + state.rows.length;
      if (state.concurrency) { text += ' · 并发上限 ' + state.concurrency; }
      if (active()) {
        text += ' · 运行 ' + state.rows.filter(row => row.status === 'running').length
          + ' · 排队 ' + state.rows.filter(row => row.status === 'queued').length;
      }
      text = (state.site ? state.site + ' · ' : state.sites?.length ? state.sites.length + ' 个站点 · ' : '')
        + (performanceRun ? '输出测速 · ' : '容量检测 · ') + text;
      if (state.started_at) { text += ' · ' + new Date(state.started_at * 1000).toLocaleString(); }
    }
    const resultHeading = node('div', null, 'capacity-results-heading'); resultHeading.append(node('h3', '测试结果'));
    const sort = node('select'); sort.setAttribute('aria-label', '结果排序');
    for (const [value, title] of [['original', '测试顺序'], ['speed', '吞吐从高到低'], ['latency', '首字从快到慢']]) {
      const option = node('option', title); option.value = value; sort.append(option);
    }
    sort.value = resultSort; sort.disabled = !state.rows.length;
    sort.addEventListener('change', () => { resultSort = sort.value; render(); }); resultHeading.append(sort); results.append(resultHeading);
    const summary = node('p', error || text, 'capacity-status'); summary.setAttribute('role', error ? 'alert' : 'status');
    summary.setAttribute('aria-live', 'polite'); results.append(summary);
    if (state.storage_error) {
      const warning = node('p', state.storage_error); warning.setAttribute('role', 'alert'); results.append(warning);
    }
    if (state.rows.length) {
      const strip = node('div', null, 'capacity-overview');
      for (const [title, value, tone] of [
        ['已完成', done + ' / ' + state.rows.length, ''],
        ['请求成功', state.rows.filter(row => row.status === 'success').length, 'positive'],
        ['失败 / 停止', state.rows.filter(row => ['failed', 'cancelled'].includes(row.status)).length, failed ? 'negative' : '']
      ]) {
        const metric = node('div', null, tone); metric.append(node('span', title), node('strong', String(value))); strip.append(metric);
      }
      results.append(strip);
      const progress = node('progress'); progress.max = state.rows.length; progress.value = done;
      progress.setAttribute('aria-label', '批量测试进度'); results.append(progress);
      const columns = node('div', null, 'capacity-columns');
      for (const title of ['站点 / 模型', '端到端吞吐', '首字延迟', '状态']) { columns.append(node('span', title)); }
      results.append(columns);
    } else {
      const empty = node('div', null, 'capacity-empty'); const symbol = window.BenchUI?.icon('chart-no-axes-combined');
      if (symbol) { empty.append(symbol); }
      empty.append(node('h4', '暂无测试结果')); results.append(empty);
    }
    if (error) {
      const retry = node('button', '刷新检测状态'); retry.type = 'button'; retry.dataset.icon = 'refresh-cw';
      retry.disabled = busy; retry.addEventListener('click', () => update('')); results.append(retry);
    }
    const rows = [...state.rows];
    // 排序只影响展示；未知值排在末尾，不把缺失用量解释为零速度。
    if (resultSort !== 'original') {
      const key = resultSort === 'speed' ? 'end_to_end_tokens_per_second' : 'ttft_seconds';
      rows.sort((a, b) => {
        const left = a.metrics?.[key], right = b.metrics?.[key];
        if (left == null) { return right == null ? 0 : 1; }
        if (right == null) { return -1; }
        return resultSort === 'speed' ? right - left : left - right;
      });
    }
    const maxSpeed = Math.max(1, ...rows.map(row => row.metrics?.end_to_end_tokens_per_second || 0));
    for (const row of rows) {
      const item = node('details', null, 'capacity-result');
      item.dataset.model = targetKey(row.site || state.site, row.model); item.open = expanded.has(item.dataset.model);
      const head = node('summary');
      const metrics = row.metrics || {};
      const resultLabel = performanceRun && row.status === 'success' && !row.capacity_detected
        ? metrics.sample_short ? '输出过短 · 未计算速度' : '请求成功' : label(row);
      const identity = node('span', null, 'capacity-identity');
      identity.append(node('strong', row.model), node('small', row.site || state.site || 'OpenAI 官方'));
      const throughput = node('span', null, 'capacity-throughput');
      if (metrics.end_to_end_tokens_per_second != null) {
        const speed = node('strong', metrics.end_to_end_tokens_per_second.toFixed(2) + ' tokens/s', 'capacity-speed');
        speed.title = '端到端输出吞吐，包含等待、思考及渠道启动耗时'; throughput.append(speed);
        const track = node('span', null, 'capacity-bar'); const fill = node('span');
        fill.style.width = Math.min(100, Math.max(0, metrics.end_to_end_tokens_per_second / maxSpeed * 100)) + '%';
        track.append(fill); throughput.append(track);
      } else { throughput.append(node('span', '—', 'capacity-unknown')); }
      // 在途时即可显示实测首字；旧版完整消息事件不能作为首字时间补入。
      const firstText = metrics.ttft_seconds ?? (row.status === 'running' && row.output_observation?.kind === 'text_delta'
        ? row.output_observation.elapsed_seconds : null);
      const latency = node('span', firstText != null ? firstText.toFixed(2) + 's' : '—', 'capacity-latency');
      latency.setAttribute('aria-label', '首字延迟');
      const status = node('span', null, 'capacity-row-state');
      const badge = node('span', resultLabel, 'capacity-badge'); badge.dataset.status = row.capacity_detected ? 'capacity' : row.status;
      status.append(badge);
      if (row.duration_seconds != null) { status.append(node('small', row.duration_seconds.toFixed(1) + 's')); }
      else if (row.status === 'running' && row.started_at) {
        status.append(node('small', '已等待 ' + Math.max(0, Date.now() / 1000 - row.started_at).toFixed(0) + 's'));
      }
      const chevron = window.BenchUI?.icon('chevron-down');
      head.append(identity, throughput, latency, status); if (chevron) { head.append(chevron); }
      item.append(head);
      if (row.status === 'running' && row.output_observation) {
        item.append(node('p', row.output_observation.kind === 'text_delta'
          ? '已收到正文，继续输出中' : '已收到完整消息，等待请求结束'));
      }
      for (const event of row.events || []) {
        item.append(node('p', '+' + event.elapsed_seconds + 's · ' + event.category));
      }
      if (row.reason) { item.append(node('p', row.reason)); }
      if (row.response_model && row.response_model !== row.model) {
        item.append(node('p', '上游返回模型：' + row.response_model));
      }
      if (row.recovery_seconds != null) { item.append(node('p', '首次容量信号后 ' + row.recovery_seconds.toFixed(1) + ' 秒请求成功')); }
      const metricList = node('dl', null, 'capacity-metrics');
      for (const [title, key, unit] of [
        ['输入 token', 'input_tokens', ''], ['输出 token', 'output_tokens', ''],
        ['推理 token', 'reasoning_tokens', ''], ['缓存输入 token', 'cached_input_tokens', ''],
        ['首字延迟', 'ttft_seconds', 's'], ['响应完成', 'response_seconds', 's'], ['首条完整消息', 'first_message_seconds', 's'],
        ['总耗时', 'total_seconds', 's']
      ]) {
        if (metrics[key] == null) { continue; }
        const pair = node('div'); pair.append(node('dt', title), node('dd', unit ? metrics[key].toFixed(2) + unit : String(metrics[key])));
        metricList.append(pair);
      }
      if (metricList.childNodes.length) { item.append(metricList); }
      if (performanceRun && row.status === 'success') {
        item.append(node('p', '端到端吞吐 = 上游输出 token ÷ 响应完成耗时；包含等待和思考，输出用量可能包含推理 token。', 'capacity-note'));
        if (row.transport === 'codex-cli' || (!row.transport && metrics.ttft_seconds == null)) {
          item.append(node('p', '本次记录未提供逐 token 时序，无法补算首字延迟；在本站登录 ChatGPT 后重新测速可采集。', 'capacity-note'));
        } else {
          item.append(node('p', '首字延迟取第一个非空正文增量；不将推理内容计入首字。', 'capacity-note'));
        }
        if (metrics.end_to_end_tokens_per_second == null && !metrics.sample_short) {
          item.append(node('p', '用量或完成时序不完整，未计算速度。', 'capacity-note'));
        }
      }
      results.append(item);
    }
    host.replaceChildren(controls, results);
    // scrollTop 和 focus 都可能强制计算布局；只允许在结果及展开状态完整后调用。
    // preventScroll 保留用户正在查看的位置，不能把焦点恢复变成自动跳转。
    if (sites.length) {
      const list = host.querySelector('.capacity-picker > .capacity-models');
      if (list) { list.scrollTop = listScroll; }
      if (focusedModel) {
        [...host.querySelectorAll('.capacity-model')].find(el => el.textContent === focusedModel)?.querySelector('input')?.focus({ preventScroll: true });
      }
      if (searchFocused) {
        const search = host.querySelector('.capacity-search input'); search.focus({ preventScroll: true });
        search.setSelectionRange(searchPosition, searchPosition);
      }
    }
  }

  /** 单次请求结束才安排轮询；错误立即展示，后台不重复发起模型检测。
   * @param {string} action 固定路由后缀：空字符串或 /cancel。
   * @param {object} [body] 省略时只读状态；存在时提交模型和模式，或取消请求。
   */
  async function update(action, body) {
    if (busy) { return; }
    clearTimeout(timer); busy = true;
    if (body !== undefined) { render(); }
    let error;
    try {
      const previous = state.id + ':' + state.status;
      state = await request(action, body);
      if (previous !== state.id + ':' + state.status) {
        document.dispatchEvent(new CustomEvent('performance-result'));
      }
      // 重载页面或重复提交时服从服务端在途模式，不能把短请求显示成长输出测速。
      if (state.mode) { mode = state.mode; }
      if (active() && Number.isInteger(state.concurrency)) { concurrencyByMode[mode] = state.concurrency; }
      if (active() && sites.length) {
        targets.clear();
        for (const row of state.rows) {
          const site = row.site || state.site;
          if (site) { targets.set(targetKey(site, row.model), { site, model: row.model }); }
        }
        selectionInitialized = true;
      }
    }
    catch (e) { error = e.message; }
    finally {
      busy = false; render(error);
      if (!error && active() && pageVisible && host?.isConnected) { timer = setTimeout(() => update(''), 1000); }
    }
  }

  /** 刷新所选站点目录；切换站点后丢弃旧响应，禁止错用另一站点的模型。
   * @param {boolean} refresh 是否由用户显式刷新，绕过服务端短时缓存。
   * @return {Promise<void>} 只读取目录，不自动发起推理请求。
   */
  async function loadModels(refresh = false) {
    const version = ++catalogVersion, name = siteSelected;
    catalogBusy = true; catalogError = ''; models = []; render();
    try {
      const response = await fetch('/api/models?site=' + encodeURIComponent(name) + (refresh ? '&refresh=1' : ''), { cache: 'no-store' });
      const data = await response.json();
      if (version !== catalogVersion) { return; }
      if (!response.ok) { throw new Error(data.error || '模型目录加载失败'); }
      models = data.ready === false ? [] : (data.models || []).filter(value => typeof value === 'string');
      catalogError = data.error || (!models.length ? '该站点暂无可用模型，请检查站点配置或登录状态。' : '');
      if (selected !== 'all' && !models.includes(selected)) {
        selected = models.includes('gpt-6.1-sol') ? 'gpt-6.1-sol' : models[0] || '';
      }
      // 只在初次打开时默认勾选一项；切换站点、刷新和用户清空均不能偷偷扩大测试范围。
      if (!selectionInitialized && models.length) {
        targets.set(targetKey(name, selected), { site: name, model: selected }); selectionInitialized = true;
      }
      if (!catalogError) {
        for (const [key, target] of targets) {
          if (target.site === name && !models.includes(target.model)) { targets.delete(key); }
        }
      }
    } catch (error) {
      if (version === catalogVersion) { catalogError = error.message; }
    } finally {
      if (version === catalogVersion) { catalogBusy = false; render(); }
    }
  }

  /** 全选当前配置中可读取的模型；失败站点单独提示，不触发任何推理请求。
   * @return {Promise<void>} 目录读取完成后恢复控件，只有点击开始才提交批次。
   */
  async function selectAllSites() {
    if (busy || active() || batchLoading) { return; }
    batchLoading = true; selectionInitialized = true; selectionError = ''; render();
    const failures = [];
    try {
      const results = await Promise.all(sites.map(async site => {
        try {
          const response = await fetch('/api/models?site=' + encodeURIComponent(site.name), { cache: 'no-store' });
          const data = await response.json();
          const available = (data.models || []).filter(model => typeof model === 'string' && model);
          if (!response.ok || data.ready === false || !available.length) { throw new Error('目录不可用'); }
          return { site: site.name, models: available };
        } catch (_) { failures.push(site.name); return null; }
      }));
      for (const result of results) {
        if (!result) { continue; }
        result.models.forEach(model => targets.set(targetKey(result.site, model), { site: result.site, model }));
        if (result.site === siteSelected) { models = result.models; catalogError = ''; }
      }
      selectionError = failures.length ? '未新增以下站点的模型：' + failures.join('、') + '。请检查站点配置或登录状态。' : '';
    } finally { batchLoading = false; render(); }
  }

  /** 从评测栏或深链接进入页面，恢复最近结果并读取站点；不会开始新的检测。
   * @return {Promise<void>} 页面就绪或展示可重试的目录错误。
   */
  async function openPage() {
    const container = document.getElementById('performance-probe');
    if (!container || pageLoading) { return; }
    pageLoading = true;
    if (!host?.isConnected) {
      mode = 'performance'; host = node('section', null, 'capacity-check'); container.append(host);
    }
    catalogBusy = true; render();
    try {
      const response = await fetch('/api/config');
      const data = await response.json();
      if (!response.ok) { throw new Error(data.error || '站点加载失败'); }
      sites = data.sites || [];
      for (const [key, target] of targets) {
        if (!sites.some(site => site.name === target.site)) { targets.delete(key); }
      }
      await update('');
      // 在途请求的站点不能被 UI 选择覆盖，已结束结果则仍允许切换下一站点。
      if (active() && state.site) { siteSelected = state.site; }
      if (!sites.some(site => site.name === siteSelected)) {
        siteSelected = sites.some(site => site.name === state.site) ? state.site : sites[0]?.name || '';
      }
      if (siteSelected) { await loadModels(); }
      else { catalogError = '尚未配置站点，请先添加站点或登录官方账号。'; }
    } catch (error) { catalogError = error.message; }
    finally { pageLoading = false; catalogBusy = false; render(); }
  }

  // 离开视图只停止页面轮询，后台任务继续；返回时恢复最新状态，避免导航取消测评。
  document.addEventListener('view-change', event => {
    pageVisible = event.detail === 'performance' && !document.getElementById('performance-probe')?.hidden;
    clearTimeout(timer);
    if (pageVisible) { openPage(); }
  });
  // 切到对比或历史只停止当前视图轮询，不取消后台测试，也不丢失模型选择。
  document.addEventListener('performance-panel-change', event => {
    pageVisible = event.detail === 'test'; clearTimeout(timer);
    if (pageVisible) { openPage(); }
  });

  // 更多操作是轻量菜单，点击外部关闭；不会清空选择或改变当前批次。
  document.addEventListener('click', event => {
    const menu = host?.querySelector('.capacity-more[open]');
    if (menu && !menu.contains(event.target)) { menu.open = false; }
  });

  window.CodexCapacity = {
    /** 独立挂载检测控件；读取现有检测，不自动消费账号额度。
     * @param {HTMLElement} container 控件所在的固定容器。
     * @param {string[]} available 当前目录模型；页面入口另外加载站点目录。
     */
    mount(container, available) {
      clearTimeout(timer);
      models = available || [];
      if (selected !== 'all' && !models.includes(selected)) {
        selected = models.includes('gpt-6.1-sol') ? 'gpt-6.1-sol' : models[0] || '';
      }
      host = node('section', null, 'capacity-check'); container.append(host); render(); update('');
    }
  };
}());
