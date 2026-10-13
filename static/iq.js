/** IQ 页面仅使用文本节点展示模型输出；不将答案作为 HTML、SVG 或脚本执行。 */
(function () {
  'use strict';
  const state = { ready: false, loading: false, picked: new Set(), sites: [], models: {}, suites: [],
    run: null, active: null, timer: null, busy: false, submitting: false, detailPair: null,
    detailKey: '', pending: null, retryPending: null, retrySubmitting: false, retryConfirmation: null, retryNotice: null,
    visible: false, runRequest: 0,
    reviewBusy: false, judgeSite: '', judgeModel: '', judgeSaving: false, judgeLoading: false,
    rankData: null, rankRequest: 0, rankMode: 'all', rankView: 'bars', rankScope: { site: '', model: '' }, rankPage: 1, rankPageSize: 10 };
  const labels = { running: '运行中', stopping: '正在停止，关闭在途请求', completed: '已完成',
    cancelled: '已停止', interrupted: '服务中断', failed: '评测失败' };
  const outcomes = { correct: '正确', incorrect: '答错', format_error: '格式错误', service_error: '接口失败',
    runner_error: '评测异常', truncated: '输出截断', empty_answer: '无答案', cancelled: '已取消', review_required: '待复核' };

  /** @param {string} id 页面固定 ID @return {HTMLElement} 对应控件 */
  function node(id) { return document.getElementById(id); }

  /**
   * 只创建文本 DOM，模型名和远端错误都不能进入 HTML 解析器。
   * @param {string} tag 标签
   * @param {string} text 可选文本
   * @param {string} cls 可选样式类
   * @return {HTMLElement} 安全节点
   */
  function element(tag, text, cls) {
    const item = document.createElement(tag);
    if (text !== undefined) { item.textContent = text; }
    if (cls) { item.className = cls; }
    return item;
  }

  /**
   * 调用同源 API；批次自身的 error 是可展示状态，不能误判为 HTTP 失败。
   * @param {string} path 固定 API 路径
   * @param {Object} body 可选 JSON 请求体
   * @return {Promise<Object>} 已解析结果
   */
  async function request(path, body) {
    const response = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined);
    const data = await response.json();
    if (!response.ok) { throw new Error(data.error || 'HTTP ' + response.status); }
    return data;
  }

  /** @param {string} message 显示或清除页面错误 */
  function error(message) {
    node('iq-error').textContent = message;
    node('iq-error').hidden = !message;
  }

  /** @param {number|null} value 可空指标 @param {number} digits 小数位 @return {string} 展示值 */
  function number(value, digits) { return value == null ? '—' : Number(value).toFixed(digits === undefined ? 1 : digits); }

  /** @param {Object} row 历史或新版摘要 @return {number|null} 百分比成绩，旧指数仅作为兼容输入 */
  function scoreOf(row) {
    if ('rank_score' in row) { return row.rank_score; }
    return row.ability_score ?? (row.iq_index == null ? null : row.iq_index / 1.5);
  }

  /**
   * 按固定百分制创建成绩条；它表示正确率，不表示任务进度，也不以页内最高分归一化。
   * @param {number} value 已通过有效性检查的复核前或复核后成绩
   * @param {string} label 无障碍名称，包含模型与评分口径
   * @return {HTMLElement} 有数值语义的横向条；真实零分保持零长度
   */
  function accuracyBar(value, label) {
    const score = Math.max(0, Math.min(100, value));
    const track = element('div', undefined, 'iq-accuracy-track');
    track.setAttribute('role', 'meter');
    track.setAttribute('aria-label', label);
    track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', '100');
    track.setAttribute('aria-valuenow', String(score));
    track.setAttribute('aria-valuetext', number(score, 2) + '%');
    const fill = element('span', undefined, 'iq-accuracy-fill');
    // 宽度只来自服务端已判定的有限数值，不能将模型名称或回答拼进样式。
    fill.style.width = score + '%';
    track.append(fill);
    return track;
  }

  /**
   * 读取完整成绩的排序值；待复核、缺失耗时和非法数值不伪装成零分。
   * @param {Object} row 同一次测评的两种成绩
   * @param {string} basis before、after 或 mean_seconds
   * @return {number|null} 有效分数或耗时
   */
  function rankValue(row, basis) {
    const result = basis === 'before' ? row.before_review : row.after_review;
    if (basis === 'after' && !result?.ready) { return null; }
    const value = basis === 'mean_seconds' ? row.mean_seconds : result?.rank_score;
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 &&
      (basis === 'mean_seconds' || value <= 100) ? value : null;
  }

  /**
   * 在整个筛选范围内计算并列名次；空值置后且不赋名次，分页不改变排名。
   * @param {Array<Object>} rows 当前筛选范围内的所有成绩
   * @param {string} basis 排名依据
   * @return {Map<Object,number>} 每行对应的名次，重复分数共享名次
   */
  function rankPositions(rows, basis) {
    const sorted = rows.filter(row => rankValue(row, basis) !== null).slice().sort((a, b) =>
      (rankValue(a, basis) - rankValue(b, basis)) * (basis === 'mean_seconds' ? 1 : -1));
    const positions = new Map(); let previous = null; let rank = 0;
    sorted.forEach((row, index) => {
      const value = rankValue(row, basis);
      if (value !== previous) { rank = index + 1; }
      previous = value; positions.set(row, rank);
    });
    return positions;
  }

  /**
   * 创建一列成绩和名次；颜色只作辅助，标题始终标明复核前后。
   * @param {Object} row 原榜单行
   * @param {string} basis before 或 after
   * @param {number|undefined} rank 当前范围内的名次，待复核时不存在
   * @param {boolean} bars 是否同时绘制百分制横条
   * @return {HTMLElement} 不含可执行模型文本的成绩单元
   */
  function comparisonScore(row, basis, rank, bars) {
    const score = rankValue(row, basis);
    const label = basis === 'before' ? '复核前' : '复核后';
    const cell = element('div', undefined, 'iq-comparison-score iq-score-' + basis);
    const heading = element('div', undefined, 'iq-comparison-caption');
    heading.append(element('span', label), element('small', rank == null ? '未排名' : '#' + rank));
    cell.append(heading, element('strong', score === null ?
      (basis === 'after' ? '待复核' : '—') : number(score, 2) + '%', score === null ? 'iq-score-pending' : ''));
    if (bars && score !== null) { cell.append(accuracyBar(score, row.site + ' / ' + row.model + ' ' + label)); }
    return cell;
  }

  /**
   * 两列同时展示同一记录的复核前后成绩，名次已在分页之前计算。
   * @param {Array<Object>} entries 本页行与所选依据的名次
   * @param {Object} positions before 和 after 的全范围名次
   * @return {HTMLElement} 可追溯来源的双列横条图
   */
  function rankBars(entries, positions) {
    const chart = element('div', undefined, 'iq-bar-chart');
    chart.setAttribute('role', 'list'); chart.setAttribute('aria-label', '正确率横向排行榜');
    const axis = element('div', undefined, 'iq-bar-axis'); axis.setAttribute('aria-hidden', 'true');
    axis.append(element('span', '排名'), element('span', '站点 / 模型'),
      element('span', '复核前'), element('span', '复核后'));
    chart.append(axis);
    entries.forEach(({ row, rank }) => {
      const item = element('div', undefined, 'iq-bar-item'); item.setAttribute('role', 'listitem');
      const identity = element('div', undefined, 'iq-bar-identity');
      identity.append(element('strong', row.model), element('small', row.site),
        element('small', '平均耗时 ' + (rankValue(row, 'mean_seconds') === null ? '—' : number(row.mean_seconds) + 's')));
      item.append(element('span', rank == null ? '—' : String(rank), 'iq-bar-rank'), identity,
        comparisonScore(row, 'before', positions.before.get(row), true),
        comparisonScore(row, 'after', positions.after.get(row), true));
      const link = element('button', undefined, 'record-action'); link.type = 'button';
      link.title = '查看记录'; link.setAttribute('aria-label', '查看 ' + row.site + ' / ' + row.model + ' 的记录');
      const icon = window.BenchUI?.icon('arrow-right');
      link.append(icon || element('span', '查看'));
      link.addEventListener('click', () => showRun(row.run_id, [row.site, row.model]).then(() =>
        node('iq-status').scrollIntoView({ block: 'start' })).catch(e => error(e.message)));
      item.append(link); chart.append(item);
    });
    return chart;
  }

  /** @param {Object} suite 题集清单 @return {Array<Object>} 分项元数据，旧记录沿用原来的四类 */
  function categoriesOf(suite) {
    return suite?.categories || ['logic', 'math', 'code', 'instruction'].map((id, i) =>
      ({ id, name: ['逻辑', '数学', '代码理解', '指令'][i] }));
  }

  /**
   * 少量分项并排列，多分项折叠，避免 BBEH 23 类撑开整个排行榜。
   * @param {HTMLElement} tr 成绩行
   * @param {Object} row 成绩摘要
   * @param {Object} suite 题集元数据
   * @param {boolean} valid 是否有完整有效成绩，缺测不展示局部分项为正式分数
   */
  function appendCategories(tr, row, suite, valid) {
    const categories = categoriesOf(suite);
    if (categories.length <= 4) {
      categories.forEach(c => tr.append(element('td', number(valid ? row.categories[c.id]?.score : null, 2))));
      return;
    }
    const cell = element('td');
    const disclosure = element('details', undefined, 'iq-category-list');
    disclosure.append(element('summary', categories.length + ' 项能力'));
    const list = element('dl');
    categories.forEach(c => {
      const result = row.categories[c.id];
      list.append(element('dt', c.name), element('dd', number(valid ? result?.score : null, 2) +
        (result ? ' · ' + result.correct + '/' + result.expected : '')));
    });
    disclosure.append(list); cell.append(disclosure); tr.append(cell);
  }

  /**
   * 同步排行榜选项，保留仍然存在的用户选择。
   * @param {HTMLSelectElement} select 目标选择器
   * @param {Array<Array<string>>} options 值和显示文本
   * @param {string} selected 希望保留的值
   * @param {string} empty 没有选项时的占位文字
   */
  function rankOptions(select, options, selected, empty) {
    select.replaceChildren();
    (options.length ? options : [['', empty]]).forEach(([value, text]) => {
      const option = element('option', text); option.value = value; select.append(option);
    });
    select.value = options.some(option => option[0] === selected) ? selected : select.options[0].value;
    select.disabled = !options.length;
  }

  /** 同时计算复核前后名次，按用户选择重排；分页和视图切换不改变成绩来源。 */
  function renderRanking() {
    if (!state.rankData) { return; }
    const data = state.rankData;
    const mode = state.rankMode;
    // 防御升级前的旧后端：无效记录只留在运行历史，不占筛选选项或分页名额。
    const validEntries = data.entries.filter(row => rankValue(row, 'before') !== null && row.complete !== false);
    node('iq-rank-scope-wrap').hidden = mode === 'all';
    if (mode !== 'all') {
      const caption = mode === 'site' ? '站点' : '模型';
      node('iq-rank-scope-label').textContent = caption;
      node('iq-rank-scope').setAttribute('aria-label', '排行榜' + caption);
      const values = Array.from(new Set(validEntries.map(row => row[mode]))).sort();
      rankOptions(node('iq-rank-scope'), values.map(value => [value, value]), state.rankScope[mode], '暂无' + caption);
      state.rankScope[mode] = node('iq-rank-scope').value;
    }
    const rows = validEntries.filter(row => mode === 'all' || row[mode] === state.rankScope[mode]);
    const basis = node('iq-rank-sort').value;
    const positions = { before: rankPositions(rows, 'before'), after: rankPositions(rows, 'after') };
    const selectedPositions = positions[basis] || rankPositions(rows, basis);
    // 排序只改变展示顺序，不触发重新选取历史记录；待复核的最终分始终排在末尾。
    rows.sort((a, b) => (rankValue(a, basis) === null) - (rankValue(b, basis) === null) ||
      ((rankValue(a, basis) ?? 0) - (rankValue(b, basis) ?? 0)) * (basis === 'mean_seconds' ? 1 : -1) ||
      a.site.localeCompare(b.site) || a.model.localeCompare(b.model));
    const ranked = positions.after.size;
    node('iq-metric-count').textContent = rows.length;
    const scoreBasis = basis === 'before' ? 'before' : 'after';
    const scores = rows.map(row => rankValue(row, scoreBasis)).filter(score => score !== null);
    node('iq-metric-score').textContent = scores.length ? number(Math.max(...scores), 2) + '%' : '—';
    const group = data.cohorts.find(item => item.id === data.cohort_id);
    const suite = group?.suite || state.suites.find(s => s.benchmark === node('iq-rank-benchmark').value && s.formal);
    const official = Boolean(suite?.benchmark);
    node('iq-metric-label').textContent = (scoreBasis === 'before' ? '复核前' : '复核后') + (official ? '最高正确率' : '最高加权分');
    node('iq-metric-suite').textContent = group ? group.suite.count + ' 题' : '—';
    node('iq-rank-status').textContent = rows.length ? rows.length + ' 个组合 · ' + ranked + ' 个已复核' +
      (rows.length > ranked ? ' · ' + (rows.length - ranked) + ' 个待复核' : '') + ' · 同分并列' : '该基准暂无完整有效成绩';
    const box = node('iq-rank-table');
    node('iq-view-bars').setAttribute('aria-pressed', String(state.rankView === 'bars'));
    node('iq-view-table').setAttribute('aria-pressed', String(state.rankView === 'table'));
    const pages = Math.max(1, Math.ceil(rows.length / state.rankPageSize));
    // 刷新后记录可能减少，将已不存在的页码移到最后一页，避免展示假空榜。
    state.rankPage = Math.min(state.rankPage, pages);
    const offset = (state.rankPage - 1) * state.rankPageSize;
    const end = Math.min(offset + state.rankPageSize, rows.length);
    node('iq-rank-pagination').hidden = !rows.length;
    node('iq-rank-range').textContent = '共 ' + rows.length + ' 条 · ' + (offset + 1) + '–' + end;
    node('iq-rank-page').value = state.rankPage;
    node('iq-rank-page').max = pages;
    node('iq-rank-pages').textContent = pages;
    node('iq-rank-prev').disabled = state.rankPage === 1;
    node('iq-rank-next').disabled = state.rankPage === pages;
    if (!rows.length) {
      box.replaceChildren(window.BenchUI?.emptyState('暂无排行榜成绩', '正式榜只收完整题集成绩；试跑见运行记录。', 'iq') || element('div', '暂无排行榜成绩', 'iq-empty'));
      return;
    }
    if (state.rankView === 'bars') {
      const entries = rows.map(row => ({ row, rank: selectedPositions.get(row) }));
      box.replaceChildren(rankBars(entries.slice(offset, end), positions));
      return;
    }
    const table = element('table', undefined, 'iq-comparison-table'); table.setAttribute('aria-label', 'IQ 排行榜成绩');
    const head = element('tr');
    ['排名', '站点', '模型', '复核前', '复核后', '平均耗时', '成绩时间', '记录'].forEach(title => {
      const th = element('th', title); th.scope = 'col'; head.append(th);
    });
    const thead = element('thead'); thead.append(head); table.append(thead);
    const body = element('tbody');
    rows.forEach((row, index) => {
      const rank = selectedPositions.get(row);
      // 页首可能与上一页末尾同分，必须保留此前行计算出的名次。
      if (index < offset || index >= end) { return; }
      const tr = element('tr');
      tr.append(element('td', rank == null ? '—' : String(rank)), element('td', row.site), element('td', row.model));
      tr.children[2].dataset.site = row.site;
      tr.children[2].setAttribute('aria-label', row.model + '，站点 ' + row.site);
      // 手机隐藏独立耗时列，按耗时排序时仍需能在模型旁核对排序数值。
      if (basis === 'mean_seconds') {
        tr.children[2].append(element('small', rankValue(row, basis) === null ? '耗时未知' :
          '平均 ' + number(row.mean_seconds) + 's', 'iq-rank-latency'));
      }
      ['before', 'after'].forEach(kind => {
        const cell = element('td'); cell.append(comparisonScore(row, kind, positions[kind].get(row), false)); tr.append(cell);
      });
      tr.append(element('td', rankValue(row, 'mean_seconds') === null ? '—' : number(row.mean_seconds) + 's'));
      tr.append(element('td', new Date((row.completed_at ?? row.started_at) * 1000).toLocaleString()));
      const link = element('button'); link.type = 'button'; link.className = 'record-action';
      link.title = '查看记录'; link.setAttribute('aria-label', '查看 ' + row.site + ' / ' + row.model + ' 的记录');
      link.append(element('span', '查看记录'));
      const arrow = window.BenchUI?.icon('arrow-right'); if (arrow) { link.append(arrow); }
      link.addEventListener('click', () => {
        showRun(row.run_id, [row.site, row.model]).then(() => node('iq-status').scrollIntoView({ block: 'start' })).catch(e => error(e.message));
      });
      const cell = element('td'); cell.append(link); tr.append(cell); body.append(tr);
    });
    table.append(body); box.replaceChildren(table);
  }

  /**
   * 翻到指定页；手输的空值、小数或越界页码会归一到有效整数页。
   * @param {number|string} value 翻页按钮或页码输入框提供的页码
   */
  function rankPage(value) {
    const page = Number(value);
    state.rankPage = Number.isFinite(page) ? Math.max(1, Math.trunc(page)) : 1;
    renderRanking();
  }

  /** 读取同批对比榜；仅多版本时显示版本选择，过期请求不能覆盖新基准。 */
  async function loadRanking() {
    const ticket = ++state.rankRequest;
    const selected = node('iq-rank-cohort').value;
    node('iq-rank-table').setAttribute('aria-busy', 'true');
    try {
      const data = await request('/api/iq/leaderboard?benchmark=' + encodeURIComponent(node('iq-rank-benchmark').value) +
        '&scoring=comparison' +
        (selected ? '&cohort=' + encodeURIComponent(selected) : ''));
      if (ticket !== state.rankRequest) { return; }
      // 前端先更新时明确报告版本不匹配，不能将旧单列成绩误标为复核后。
      if (data.scoring !== 'comparison') { throw new Error('请更新后端后查看复核前后成绩'); }
      if (state.rankData && state.rankData.cohort_id !== data.cohort_id) { state.rankPage = 1; }
      state.rankData = data;
      rankOptions(node('iq-rank-cohort'), data.cohorts.map(group => [group.id,
        (group.suite.name || group.suite.count + ' 题') + ' · ' + group.suite.version.slice(0, 12) +
        ' · ' + group.id.slice(0, 6)]), data.cohort_id, '暂无有效成绩');
      node('iq-rank-version-wrap').hidden = data.cohorts.length <= 1;
      const group = data.cohorts.find(item => item.id === data.cohort_id);
      node('iq-rank-condition').textContent = group ? '每个模型取最新有效成绩 · 两列来自同一次评测 · 运行预算可能不同' : '';
      node('iq-rank-error').hidden = true;
      renderRanking();
    } catch (e) {
      if (ticket !== state.rankRequest) { return; }
      // 条件切换失败不能继续显示上一个榜单，否则标题和分数可能不属于同一条件。
      state.rankData = null;
      ['count', 'score', 'suite'].forEach(name => { node('iq-metric-' + name).textContent = '—'; });
      node('iq-rank-pagination').hidden = true;
      node('iq-rank-table').replaceChildren(); node('iq-rank-condition').textContent = '';
      node('iq-rank-status').textContent = '排行榜未加载';
      node('iq-rank-error').textContent = e.message; node('iq-rank-error').hidden = false;
    } finally {
      if (ticket === state.rankRequest) { node('iq-rank-table').setAttribute('aria-busy', 'false'); }
    }
  }

  /** 更新调用预算和可操作状态；选模型不能解除运行中的重复提交保护。 */
  function plan() {
    const count = state.picked.size;
    const suite = state.suites.find(s => s.id === node('iq-suite').value);
    const repeats = Number(node('iq-repeats').value);
    node('iq-thinking').disabled = !suite?.benchmark;
    node('iq-suite-description').textContent = suite?.description || '自编基础题，沿用历史题目和权重。';
    node('iq-plan').textContent = count ? count + ' 个组合 × ' + (suite ? suite.count : 0) + ' 题 × ' + repeats + ' 次 = ' +
      (count * (suite ? suite.count : 0) * repeats) + ' 次请求 · temperature 0' : '尚未选择模型';
    if (state.busy) { node('iq-plan').textContent = '有评测正在运行'; }
    node('iq-start').disabled = !state.ready || !suite || !count || state.busy || state.submitting || state.loading;
    node('iq-fields').disabled = state.busy || state.submitting;
    const choices = node('iq-models').querySelectorAll('input[type=checkbox]');
    node('iq-select-all').disabled = !state.ready || state.loading || !choices.length || count === choices.length;
    node('iq-select-none').disabled = !state.ready || state.loading || !count;
    (node('iq-start').querySelector('span') || node('iq-start')).textContent = state.submitting ? '正在创建…' : '开始评测';
    retryControl();
  }

  /** 同步重试入口；旧后端没有能力字段时不显示，查看历史也不能绕过在途任务锁。 */
  function retryControl() {
    const count = state.run?.retryable_errors || 0;
    const retry = state.run?.retry;
    const running = Boolean(retry && ['running', 'stopping'].includes(state.run.status));
    node('iq-retry').hidden = !count && !running && !state.retrySubmitting;
    node('iq-retry').disabled = state.busy || state.submitting || state.retrySubmitting ||
      ['running', 'stopping'].includes(state.run?.status);
    node('iq-retry').setAttribute('aria-busy', String(state.retrySubmitting || running));
    node('iq-retry').querySelector('span').textContent = state.retrySubmitting ? '正在提交重试…' :
      running ? '重试中（' + retry.done + ' / ' + retry.count + '）' : '重试接口失败（' + count + '）';
    retryFeedback();
  }

  /**
   * 单独展示重试提交、运行和结束状态；旧结果仍在列表中，不能用总题数作为本轮进度。
   * 通知绑定原记录 ID，切换历史不串提示；显示自动轮次与等待时间，读取不重发请求。
   */
  function retryFeedback() {
    const run = state.run; const retry = run?.retry;
    const confirm = state.retryConfirmation;
    const validConfirm = confirm && confirm.runId === run?.run_id && !state.busy &&
      !['running', 'stopping'].includes(run?.status) && run.retryable_errors;
    node('iq-retry-confirm').hidden = !validConfirm;
    if (validConfirm) {
      node('iq-retry-confirm-description').textContent = '仅重试 ' + run.retryable_errors +
        ' 项接口失败，结果写回当前评测，不新建任务。仍失败时自动再试至多 3 次，会消耗模型额度或产生费用。';
    } else if (confirm && !state.retrySubmitting) {
      // 确认期间后台状态或历史选择已变化，不能把旧确认用于另一轮任务。
      state.retryConfirmation = null;
    }
    const notice = state.retryNotice?.runId === run?.run_id ? state.retryNotice : null;
    let title = notice?.title || ''; let description = notice?.description || ''; let kind = notice?.kind || '';
    if (!notice && retry) {
      const active = ['running', 'stopping'].includes(run.status);
      // 旧版重试子记录未必包含单独的时间字段，沿用其批次时间，避免显示 NaN 秒。
      const started = retry.started_at || run.started_at;
      const finished = active ? Date.now() / 1000 : retry.finished_at || run.finished_at || started;
      const seconds = Math.max(0, Math.floor(finished - started));
      kind = active ? 'running' : 'completed';
      title = active ? (run.status === 'stopping' ? '正在停止重试' : '正在原地重试') :
        run.status === 'completed' ? '本次重试已结束' : '本次重试' + (labels[run.status] || run.status);
      if (retry.trigger === 'automatic') {
        title += ' · 自动重试 ' + retry.auto_round + ' / ' + retry.auto_max_rounds;
      }
      title += ' · ' + retry.done + ' / ' + retry.count + ' 项';
      description = '已用 ' + seconds + ' 秒 · 保留 ' + retry.retained + ' 个原回答。';
      if (active) {
        const delay = Math.max(0, Math.ceil((retry.next_attempt_at || 0) - Date.now() / 1000));
        description += run.status === 'stopping' ? ' 已停止继续调度，不再自动重试。' :
          delay > 0 ? ' ' + delay + ' 秒后开始本轮请求。' :
          retry.done >= retry.count ? ' 正在保存本轮结果。' :
          (retry.done === 0 ? ' 正在等待模型返回；' : ' 其余请求仍在进行；') +
          '单项总时限 ' + run.options.timeout_seconds + ' 秒。';
      } else {
        description += ' 当前记录仍有 ' + (run.retryable_errors || 0) + ' 项接口失败。';
        if (run.status === 'completed' && run.retryable_errors && run.auto_retry?.round >= run.auto_retry?.max_rounds) {
          description += ' 已达自动重试上限 ' + run.auto_retry.max_rounds + ' 次，可手动重试。';
        }
      }
    }
    node('iq-retry-feedback').hidden = !title;
    node('iq-retry-feedback').dataset.state = kind;
    node('iq-retry-feedback').setAttribute('aria-busy', String(kind === 'submitting' || kind === 'running'));
    node('iq-retry-feedback-title').textContent = title;
    node('iq-retry-feedback-description').textContent = description;
    retryItems();
  }

  /**
   * 显示当前重试轮的逐项状态；耗时只从实际开始时间计算，旧失败不冒充新进度。
   * 同一轮复用表格行，避免轮询时重置滚动位置或用户展开的错误详情。
   */
  function retryItems() {
    const run = state.run; const retry = run?.retry; const items = retry?.items || [];
    node('iq-retry-items').hidden = !items.length;
    const box = node('iq-retry-items-table');
    if (!items.length) { box.replaceChildren(); delete box.dataset.signature; return; }
    const active = ['running', 'stopping'].includes(run.status);
    const queued = items.filter(item => item.status === 'queued').length;
    const running = items.filter(item => item.status === 'running').length;
    const ended = items.filter(item => !['queued', 'running', 'unknown'].includes(item.status)).length;
    node('iq-retry-items-title').textContent = retry.trigger === 'automatic' ?
      '自动重试 ' + retry.auto_round + ' / ' + retry.auto_max_rounds + ' 明细' :
      (retry.round ? '第 ' + retry.round + ' 轮' : '本轮') + '重试明细';
    // 结束后的旧快照可能缺少逐项收尾事件，不能仍把这些项统计成在途请求。
    node('iq-retry-items-count').textContent = active ?
      '请求中 ' + running + ' · 排队 ' + queued + ' · 已结束 ' + ended + ' / ' + items.length :
      '本轮已结束 · 共 ' + items.length + ' 项';
    const signature = JSON.stringify([run.run_id, retry.request_id, items.map(item => [item.site, item.model, item.question_id, item.repeat])]);
    if (box.dataset.signature !== signature) {
      const table = element('table', undefined, 'iq-retry-table');
      const head = element('thead'); const headings = element('tr');
      ['模型 / 站点', '题目 / 次数', '状态', '耗时', '进度详情'].forEach(text => {
        const th = element('th', text); th.scope = 'col'; headings.append(th);
      });
      head.append(headings); table.append(head);
      const body = element('tbody');
      items.forEach(item => {
        const row = element('tr');
        const model = element('td'); const identity = element('div', undefined, 'iq-identity');
        identity.append(element('strong', item.model), element('small', item.site)); model.append(identity);
        const question = element('td');
        question.append(element('span', item.question_id), element('small', '第 ' + item.repeat + ' 次'));
        const status = element('td'); const duration = element('td');
        const detail = element('td'); const disclosure = element('details');
        disclosure.append(element('summary'), element('p', '', 'muted'));
        detail.append(disclosure); row.append(model, question, status, duration, detail); body.append(row);
      });
      table.append(body); box.replaceChildren(table); box.dataset.signature = signature;
    }
    const now = Date.now() / 1000;
    Array.from(box.querySelectorAll('tbody tr')).forEach((row, index) => {
      const item = items[index]; const cells = row.children;
      // 请求中的秒数表示已经等待多久，并不代表模型完成百分比。
      const elapsed = item.status === 'running' && active && item.started_at ? Math.max(0, now - item.started_at) : item.duration_seconds;
      const label = item.status === 'running' ? (run.status === 'stopping' ? '正在取消' : active ? '请求中' : '已中断') :
        ({ queued: active ? '排队' : '未执行', unknown: '未记录', interrupted: '已中断' }[item.status] || outcomes[item.status] || item.status);
      cells[2].textContent = label;
      cells[2].className = 'iq-result-' + item.status;
      cells[3].textContent = elapsed == null ? (item.status === 'queued' ? '未开始' : '未记录') : number(elapsed, 1) + 's';
      let detail = item.reason || label;
      if (item.status === 'queued' && active) { detail = '等待并发名额'; }
      if (item.status === 'running' && active) {
        const remaining = elapsed == null ? null : Math.max(0, Math.ceil(run.options.timeout_seconds - elapsed));
        detail = run.status === 'stopping' ? '正在关闭请求，等待收尾' :
          '等待完整响应' + (remaining == null ? '' : ' · 距总时限 ' + remaining + 's');
      }
      const disclosure = cells[4].querySelector('details');
      const summary = disclosure.querySelector('summary');
      summary.textContent = detail; summary.title = detail;
      const timing = item.started_at ? '请求开始：' + new Date(item.started_at * 1000).toLocaleString() : '请求开始：未记录';
      const finished = item.finished_at ? '；请求结束：' + new Date(item.finished_at * 1000).toLocaleString() : '';
      disclosure.querySelector('p').textContent = timing + finished + '。' + detail;
    });
  }

  /**
   * 批量修改选择并更新请求数；包含搜索暂时隐藏的模型，不发起评测请求。
   * @param {HTMLElement} scope 全部模型列表或单个站点的选项容器
   * @param {boolean} selected true 为全选，false 为取消全选
   */
  function selectModels(scope, selected) {
    // 运行中或刷新列表时不改变本次选择，避免显示预算与已提交任务不一致。
    if (!state.ready || state.loading || state.busy || state.submitting) { return; }
    scope.querySelectorAll('input[type=checkbox]').forEach(checkbox => {
      checkbox.checked = selected;
      if (selected) { state.picked.add(checkbox.dataset.key); }
      else { state.picked.delete(checkbox.dataset.key); }
    });
    plan();
  }

  /**
   * 展示保存配置、历史画廊或模型列表中的候选，不自动选择或调用模型。
   * @param {Object} failures 按站点归集的拉取失败消息
   */
  function renderModels(failures) {
    const box = node('iq-models');
    box.replaceChildren();
    const known = new Set();
    state.sites.forEach(site => {
      const section = element('div', undefined, 'iq-site');
      const head = element('div', undefined, 'iq-site-head');
      head.append(element('strong', site.name), element('span', (state.models[site.name] || []).length + ' 个模型', 'muted'));
      const search = element('input');
      search.type = 'search'; search.placeholder = '筛选模型'; search.setAttribute('aria-label', '筛选 ' + site.name + ' 的模型');
      head.append(search); section.append(head);
      if (site.note || site.error) { section.append(element('p', site.error || site.note, 'muted')); }
      if (failures && failures[site.name]) { section.append(element('p', failures[site.name], 'iq-error')); }
      const choices = element('div', undefined, 'iq-choices');
      const selectAll = element('button', '全选本站'); selectAll.type = 'button';
      selectAll.setAttribute('aria-label', '全选 ' + site.name + ' 的模型');
      const selectNone = element('button', '取消全选'); selectNone.type = 'button';
      selectNone.setAttribute('aria-label', '取消全选 ' + site.name + ' 的模型');
      selectAll.addEventListener('click', () => selectModels(choices, true));
      selectNone.addEventListener('click', () => selectModels(choices, false));
      head.append(selectAll, selectNone);
      (state.models[site.name] || []).forEach(model => {
        const key = JSON.stringify([site.name, model]);
        known.add(key);
        const label = element('label');
        const checkbox = element('input');
        checkbox.type = 'checkbox'; checkbox.checked = state.picked.has(key);
        checkbox.dataset.key = key;
        checkbox.addEventListener('change', () => { if (checkbox.checked) { state.picked.add(key); } else { state.picked.delete(key); } plan(); });
        label.append(checkbox, element('span', model)); choices.append(label);
        label.dataset.search = model.toLowerCase();
      });
      search.addEventListener('input', () => {
        choices.querySelectorAll('label').forEach(label => { label.hidden = !label.dataset.search.includes(search.value.toLowerCase()); });
      });
      section.append(choices);
      selectAll.disabled = selectNone.disabled = !choices.childElementCount;
      if (!choices.childElementCount) { section.append(element('p', '暂无模型列表', 'muted')); }
      box.append(section);
    });
    if (!state.sites.length) { box.append(element('p', '尚未配置模型站点', 'muted')); }
    // 站点被移除或重命名时，丢弃失效选项，防止显示计数与实际任务不符。
    for (const key of state.picked) { if (!known.has(key)) { state.picked.delete(key); } }
    plan();
  }

  /** 只拉取候选模型；失败时保留本地已知列表，不把空列表覆盖掉。 */
  async function loadModels() {
    if (state.loading) { return; }
    state.loading = true; plan(); node('iq-load-models').disabled = true; error('');
    try {
      const saved = await request('/api/config');
      state.sites = saved.sites;
      const failures = {};
      await Promise.all(state.sites.map(async site => {
        try {
          const result = await request('/api/models?site=' + encodeURIComponent(site.name));
          if (result.models && result.models.length) { state.models[site.name] = result.models; }
          if (result.error) { failures[site.name] = result.error; }
        } catch (e) { failures[site.name] = e.message; }
      }));
      renderModels(failures); judgeOptions();
    } catch (e) { error(e.message); }
    finally { state.loading = false; node('iq-load-models').disabled = false; plan(); }
  }

  /**
   * 更新历史列表，同时识别后台批次；查看旧成绩不能误解除新批次的运行锁。
   * @return {Promise<void>} 列表加载完成
   */
  async function histories() {
    const data = await request('/api/iq/runs');
    state.active = data.runs.find(run => ['running', 'stopping'].includes(run.status)) || null;
    state.busy = Boolean(state.active);
    node('iq-active-run').hidden = !state.active;
    if (state.active) {
      node('iq-active-run').textContent = state.active.retry ?
        '接口失败重试中 · ' + state.active.retry.done + ' / ' + state.active.retry.count + ' 项 · 查看进度' :
        'IQ 评测运行中 · 查看当前进度';
    }
    const selected = state.run ? state.run.run_id : (state.active ? state.active.run_id : (data.runs[0] || {}).run_id);
    // 排行榜可指向最近五十批之外的记录；保留当前选中项，避免下次轮询跳走。
    if (state.run && !data.runs.some(run => run.run_id === state.run.run_id)) { data.runs.push(state.run); }
    node('iq-history').replaceChildren();
    data.runs.forEach(run => {
      const option = element('option', new Date(run.started_at * 1000).toLocaleString() + ' · ' + (run.suite.name || run.suite.count + '题') +
        (run.retry ? ' · 失败项重试' : '') + ' · ' + labels[run.status]);
      option.value = run.run_id; node('iq-history').append(option);
    });
    if (!data.runs.length) {
      const empty = element('option', '暂无评测'); empty.value = '';
      node('iq-history').append(empty);
    }
    node('iq-history').value = selected || '';
    plan();
  }

  /**
   * 生成成绩表；不完整批次只展示覆盖率，绝不把剩余未做题当成已判分题。
   * @param {Object} run 服务端摘要
   */
  function render(run) {
    if (state.retryNotice?.runId === run.run_id && state.retryNotice.kind === 'poll-error') {
      // 成功重新读取后撤销过期的网络提示；原地重试失败的提交提示仍需用户确认处理。
      state.retryNotice = null;
    }
    if (state.retryPending?.source === run.run_id && [run.retry, ...(run.retry_history || [])]
      .some(retry => retry?.request_id === state.retryPending.id)) {
      // POST 响应丢失后，手动轮次可能已进入历史并开始自动重试。
      // 同时检查历史才能确认已接受，避免旧错误提示遮住当前进度。
      state.retryPending = null; state.retryNotice = null;
    }
    state.run = run;
    const pendingReviews = run.summary.reduce((sum, row) => sum + (row.final?.pending || 0), 0);
    const reviewFailures = run.summary.reduce((sum, row) => sum + (row.final?.review_failed || 0), 0);
    const inReview = ['queued', 'running'].includes(run.auto_review?.status);
    const statusLabel = run.status === 'completed' && pendingReviews ?
      '答题完成 · ' + (inReview ? '复核中' : reviewFailures ? '复核失败' : '待复核') : labels[run.status];
    node('iq-status').textContent = statusLabel + ' · ' + (run.suite.name || '') +
      (run.suite.formal === false ? ' · 试跑，不入正式榜' : '') + ' · ' + run.done + ' / ' + run.total + ' 次 · ' + run.suite.version.slice(0, 12) +
      ' · 每题 ' + run.options.repeats + ' 次 · 并发 ' + run.options.concurrency +
      ' · 请求总时限 ' + run.options.timeout_seconds + 's' + (run.error ? ' · ' + run.error : '');
    if (run.configurations?.some(c => c.execution && c.execution.transport !== 'kimi-acp')) {
      node('iq-status').textContent += ' · Codex：medium，超时按单题总时限，API 温度及输出上限不适用';
    }
    if (run.configurations?.some(c => c.execution?.transport === 'kimi-acp')) {
      node('iq-status').textContent += ' · Kimi：默认推理，超时按单题总时限，API 温度及输出上限不适用';
    }
    if (run.retry) {
      node('iq-status').textContent += ' · 本次重试 ' + run.retry.done + ' / ' + run.retry.count + ' 项 · 保留 ' + run.retry.retained + ' 个回答';
    }
    if (run.auto_retry) {
      node('iq-status').textContent += ' · 自动重试 ' + run.auto_retry.round + ' / ' + run.auto_retry.max_rounds + ' 轮';
    }
    // 请求已经收尾不等于成绩有效；明确解释空分数，避免 840/840 让用户误以为丢失结果。
    if (!['running', 'stopping'].includes(run.status)) {
      const failed = run.summary.reduce((sum, row) => sum + (row.service_errors || 0), 0);
      const pending = pendingReviews;
      if (failed) { node('iq-status').textContent += ' · ' + failed + ' 项接口失败，有缺测的模型暂无正式成绩'; }
      if (pending) { node('iq-status').textContent += ' · ' + pending + ' 条答案未复核完，暂不发布对应模型的最终成绩'; }
    }
    const auto = run.auto_review;
    state.reviewBusy = inReview;
    if (auto && !inReview && !pendingReviews && run.summary.length && run.summary.every(row => row.final)) {
      // 自动批次保留当时的失败记录；逐题补复核后用当前结论显示完成，避免旧错误
      // 仍挂在已经有效的最终分旁边。接口缺测在上方独立说明，不由复核状态掩盖。
      const reviewed = run.summary.reduce((sum, row) => sum + row.final.reviewed, 0);
      node('iq-status').textContent += ' · 复核完成：Agent 已复核 ' + reviewed + ' 条';
    } else if (auto) {
      const reviewLabels = { queued: '排队中', running: '进行中', completed: '完成', partial: '部分完成',
        interrupted: '已中断', failed: '失败', unavailable: '无可用模型' };
      node('iq-status').textContent += ' · 自动复核' + (reviewLabels[auto.status] || auto.status) +
        '：规则检查 ' + auto.rule_checked + ' 条，Agent ' + auto.done + ' / ' + auto.total;
      if (auto.failed) { node('iq-status').textContent += '，失败 ' + auto.failed; }
      if (auto.reused) { node('iq-status').textContent += '，复用 ' + auto.reused + ' 条复核'; }
      if (auto.deferred) { node('iq-status').textContent += '，另 ' + auto.deferred + ' 条需手动复核'; }
      if (auto.error) { node('iq-status').textContent += ' · ' + auto.error; }
    } else if (run.auto_review_error) {
      node('iq-status').textContent += ' · ' + run.auto_review_error;
    }
    // 原地重试没有第二条历史记录；旧版子任务记录仍可通过这个入口回看。
    node('iq-retry-source').hidden = !run.retry || run.retry.mode === 'in_place';
    retryControl();
    // 原地重试保留旧失败结果，总完成数会继续是 840/840；进度条必须使用本轮的 done/count。
    node('iq-progress').hidden = false;
    node('iq-progress').max = run.retry ? run.retry.count : run.total;
    node('iq-progress').value = run.retry ? run.retry.done : run.done;
    node('iq-progress').setAttribute('aria-label', run.retry ? '重试进度' : '评测进度');
    if (inReview && !['running', 'stopping'].includes(run.status)) {
      node('iq-progress').max = Math.max(1, auto.total);
      node('iq-progress').value = auto.done;
      node('iq-progress').setAttribute('aria-label', '自动复核进度');
    }
    node('iq-export').disabled = false;
    node('iq-stop').hidden = !['running', 'stopping'].includes(run.status);
    node('iq-stop').disabled = run.status === 'stopping';
    const table = element('table');
    table.setAttribute('aria-label', 'IQ 评测成绩');
    const header = element('tr');
    const categories = categoriesOf(run.suite);
    judgeOptions();
    ['站点 / 模型', '最终正确率 / %', '已复核 / 待复核', '正确 / 答错', '接口失败 / 已取消 / 未执行',
      ...(categories.length <= 4 ? categories.map(c => c.name) : ['分项成绩']), '有效 / 计划', '均耗时', '总耗时', '费用估算', ''].forEach(title => {
      const th = element('th', title); th.scope = 'col'; header.append(th);
    });
    const thead = element('thead'); thead.append(header); table.append(thead);
    const tbody = element('tbody');
    run.summary.forEach(summary => {
      const row = element('tr');
      const identity = element('td'); const text = element('div', undefined, 'iq-identity');
      text.append(element('strong', summary.model), element('small', summary.site)); identity.append(text); row.append(identity);
      const final = summary.final;
      const score = element('td', final?.ability_score != null ? number(final.ability_score, 2) :
        final?.pending ? (final.review_failed ? '复核失败' : inReview ? '复核中' : '待复核') : '—', 'iq-index');
      score.title = '题目完整、无接口缺测且未通过的完整回答复核完毕后发布';
      const audit = element('details', undefined, 'muted');
      audit.append(element('summary', '初评记录'), element('p', '原始评分：' + number(summary.ability_score, 2) +
        '；规则规范化：' + number(summary.normalized?.ability_score, 2)));
      score.append(audit);
      if (typeof final?.ability_score === 'number' && Number.isFinite(final.ability_score)) {
        score.classList.add('iq-score-with-bar');
        score.append(accuracyBar(final.ability_score, summary.model + ' 最终正确率'));
      }
      row.append(score);
      row.append(element('td', (final?.reviewed || 0) + ' / ' + (final?.pending || 0)));
      row.append(element('td', (final?.correct ?? summary.correct) + ' / ' + (final?.incorrect ?? summary.incorrect)),
        element('td', summary.service_errors + ' / ' + (summary.cancelled || 0) + ' / ' + summary.missing));
      appendCategories(row, { ...summary, ...final }, run.suite, final?.ability_score != null);
      row.append(element('td', summary.valid + ' / ' + summary.expected), element('td', number(summary.mean_seconds) + (summary.mean_seconds == null ? '' : 's')));
      row.append(element('td', number(summary.elapsed_seconds) + 's'), element('td', summary.known_cost_usd == null ? '未知' : (summary.cost_complete ? '$' : '已知 $') + number(summary.known_cost_usd, 4)));
      const cell = element('td'); const button = element('button', '逐题结果');
      button.type = 'button'; button.disabled = summary.completed === 0;
      button.addEventListener('click', () => {
        state.detailPair = [summary.site, summary.model]; state.detailKey = '';
        details().catch(e => error(e.message));
      });
      cell.append(button); row.append(cell); tbody.append(row);
    });
    table.append(tbody); node('iq-summary').replaceChildren(table);
  }

  /** 按需获取完整回答，保留已展开条目；所有题干和输出以纯文本呈现。 */
  async function details() {
    if (!state.run || !state.detailPair) { return; }
    const runId = state.run.run_id;
    const pair = state.detailPair.slice();
    const key = runId + JSON.stringify(pair) + state.run.done + ':' + (state.run.result_revision || 0) +
      ':' + (state.run.auto_review?.done || 0) + ':' + (state.run.auto_review?.status || '');
    if (key === state.detailKey) { return; }
    const run = await request('/api/iq/run/' + runId + '?full=1');
    const reviews = run.normalization_version ? await request('/api/iq/reviews?run_id=' + runId) : { reviews: [] };
    // 网络返回时用户可能已经切换批次或模型，旧结果不能覆盖新选择。
    if (!state.run || state.run.run_id !== runId || JSON.stringify(state.detailPair) !== JSON.stringify(pair)) { return; }
    state.detailKey = key;
    const box = node('iq-details');
    const opened = new Set(Array.from(box.querySelectorAll('details[open]')).map(d => d.dataset.key));
    box.replaceChildren();
    const heading = element('div', undefined, 'iq-details-heading');
    heading.append(element('h3', pair[0] + ' / ' + pair[1] + ' · 逐题记录'));
    const filter = element('select'); filter.setAttribute('aria-label', '筛选答题结果');
    [['all', '全部结果'], ['errors', '答错 / 格式 / 截断'], ['review', '待复核'], ['service', '接口失败 / 评测异常'], ['cancelled', '已取消'], ['correct', '正确']].forEach(entry => { const o = element('option', entry[1]); o.value = entry[0]; filter.append(o); });
    heading.append(filter); box.append(heading);
    const questions = Object.fromEntries(run.questions.map(q => [q.id, q]));
    run.results.filter(r => r.site === pair[0] && r.model === pair[1]).sort((a, b) => a.question_id.localeCompare(b.question_id) || a.repeat - b.repeat).forEach(result => {
      const q = questions[result.question_id];
      const item = element('details', undefined, 'iq-detail');
      item.dataset.key = result.question_id + '-' + result.repeat;
      const status = result.final?.status || result.normalized?.status || result.status;
      item.dataset.status = status; item.open = opened.has(item.dataset.key);
      const title = element('summary');
      title.append(element('span', outcomes[status], 'iq-result-' + status), element('span', q.title),
        element('span', q.id + ' · 第 ' + result.repeat + ' 次 · ' + number(result.duration_seconds) + 's', 'iq-detail-meta'));
      item.append(title, element('h4', '题目'), element('pre', q.prompt));
      const answers = element('div', undefined, 'iq-answer');
      const actual = element('div'); actual.append(element('h4', '模型回答'), element('pre', result.raw || '（无正文）'));
      const expected = element('div'); expected.append(element('h4', '标准答案'), element('pre', JSON.stringify(q.expected, null, 2)));
      answers.append(actual, expected); item.append(answers, element('p', result.reason, 'muted'));
      if (result.normalized) {
        item.append(element('p', '规则规范化：' + outcomes[result.normalized.status] + ' · ' + result.normalized.reason, 'muted'));
        if (result.final) { item.append(element('p', '最终判定：' + outcomes[status] + ' · ' + result.final.reason, 'iq-result-' + status)); }
        if (result.normalized.extracted != null) {
          item.append(element('h4', '提取答案'), element('pre', JSON.stringify(result.normalized.extracted, null, 2)));
        }
        if (result.score === 0 && result.normalized.score === 1) {
          item.append(element('p', '答案已识别为正确，原始评分未通过。', 'muted'));
        }
        const review = reviews.reviews.find(r => r.id === result.final?.review_id);
        if (review || (result.final?.source === 'agent' && status !== 'correct')) {
          appendReview(item, runId, result, review);
        }
      }
      if (result.response_model) { item.append(element('p', '上游返回模型：' + result.response_model, 'muted')); }
      box.append(item);
    });
    filter.addEventListener('change', () => {
      box.querySelectorAll('.iq-detail').forEach(item => {
        const service = ['service_error', 'runner_error'].includes(item.dataset.status);
        item.hidden = filter.value === 'correct' ? item.dataset.status !== 'correct' :
          filter.value === 'review' ? item.dataset.status !== 'review_required' :
          filter.value === 'cancelled' ? item.dataset.status !== 'cancelled' :
          filter.value === 'service' ? !service : filter.value === 'errors' && (service || ['correct', 'cancelled'].includes(item.dataset.status));
      });
    });
  }

  /** 同步复核站点和模型选项；仅展示候选，不自动发起付费复核。 */
  function judgeOptions() {
    const site = node('iq-judge-site');
    rankOptions(site, state.sites.map(s => [s.name, s.name]), site.value || state.judgeSite, '暂无站点');
    // 初始手动复核沿用自动复核渠道，不能因配置中的站点顺序而默认换成另一个收费渠道。
    // /models 缓存尚未保存时仍允许使用服务端明确配置的评委；权限以实际调用结果为准。
    const models = Array.from(new Set((state.models[site.value] || []).concat(
      (state.run?.configurations || []).filter(c => c.site === site.value).map(c => c.model),
      site.value === state.judgeSite && state.judgeModel ? [state.judgeModel] : []))).sort();
    rankOptions(node('iq-judge-model'), models.map(m => [m, m]),
      node('iq-judge-model').value || (site.value === state.judgeSite ? state.judgeModel : ''), '请刷新模型');
    judgeSettingsState();
  }

  /** 同步保存状态；草稿和已保存配置分开，保存失败不能让页面误报新的自动复核模型。 */
  function judgeSettingsState() {
    const site = node('iq-judge-site'); const model = node('iq-judge-model');
    const locked = state.judgeSaving || state.judgeLoading;
    site.disabled = locked || !state.sites.length;
    model.disabled = locked || !model.value;
    node('iq-judge-refresh').disabled = locked || !site.value;
    node('iq-judge-save').disabled = locked || !site.value || !model.value;
    node('iq-judge-current').textContent = state.judgeSite && state.judgeModel ?
      '当前自动复核：' + state.judgeSite + ' / ' + state.judgeModel : '尚未配置';
  }

  /** 标记用户修改的复核草稿；尚未保存时只影响手动复核，不替换自动复核配置。 */
  function judgeChanged() {
    judgeOptions();
    node('iq-review-error').hidden = true;
    node('iq-judge-save-status').textContent = node('iq-judge-site').value === state.judgeSite &&
      node('iq-judge-model').value === state.judgeModel ? '' : '尚未保存';
  }

  /**
   * 显式保存两个公开字段，不回传整份站点配置，避免旧页面覆盖凭据和其他参数。
   * 同一时间只提交一次；失败保留选择，已提交的复核继续使用原评委。
   * @return {Promise<void>} 保存完成或错误已展示后返回
   */
  async function saveJudgeSettings() {
    if (state.judgeSaving || state.judgeLoading) { return; }
    const site = node('iq-judge-site').value; const model = node('iq-judge-model').value;
    if (!site || !model) { return; }
    state.judgeSaving = true; judgeSettingsState();
    node('iq-review-error').hidden = true; node('iq-judge-save-status').textContent = '正在保存…';
    try {
      const saved = await request('/api/iq/review-settings', { iq_auto_review_site: site, iq_auto_review_model: model });
      // 只有服务端成功落盘后才更改已保存身份，避免失败或连接中断时显示虚假的成功状态。
      state.judgeSite = saved.iq_auto_review_site; state.judgeModel = saved.iq_auto_review_model;
      node('iq-judge-save-status').textContent = '已保存，用于下一次评测和重试。';
    } catch (e) {
      node('iq-review-error').textContent = e.message; node('iq-review-error').hidden = false;
      node('iq-judge-save-status').textContent = '保存失败，选择已保留';
    } finally { state.judgeSaving = false; judgeSettingsState(); }
  }

  /**
   * 显示后端校验后的复核结论，旧版参考建议单独标记；模型文本只能按纯文本展示。
   * @param {HTMLElement} panel 当前题目的复核区域
   * @param {Object} job 服务端复核状态
   */
  function renderReview(panel, job) {
    panel.replaceChildren(element('h4', '评分 Agent · ' + job.judge_site + ' / ' + job.judge_model));
    if (job.status === 'running') { panel.append(element('p', '正在复核…')); return; }
    if (job.status !== 'completed') { panel.append(element('p', job.error || '复核未完成', 'iq-error')); return; }
    panel.append(element('p', (job.advisory === false ? '复核结论：' : '历史建议：') + (outcomes[job.suggestion.status] || '待复核') + ' · ' + job.extraction.reason));
    if (job.extraction.answer != null) { panel.append(element('p', '提取答案：' + job.extraction.answer)); }
    if (job.extraction.evidence) { panel.append(element('h4', '原文证据'), element('pre', job.extraction.evidence)); }
    const cost = job.cost_estimate_usd == null ? '费用未知' : '$' + number(job.cost_estimate_usd, 4);
    panel.append(element('p', (job.advisory === false ? '已用于最终判定' : '旧版建议，不计入成绩') + ' · ' + job.version + ' · ' + number(job.duration_seconds) + 's · ' + cost, 'muted'));
  }

  /**
   * 轮询一个已提交任务，页面离开或条目重建后停止更新旧 DOM。
   * @param {HTMLElement} panel 对应题目的复核容器
   * @param {Object} job 初始任务状态
   * @param {Function} settled 结束或网络异常时通知按钮，异常时保留幂等请求 ID
   * @return {Promise<void>} 当前轮询完成
   */
  async function pollReview(panel, job, settled) {
    renderReview(panel, job);
    if (job.status !== 'running') { settled(job); return; }
    setTimeout(async () => {
      if (!panel.isConnected) { return; }
      try {
        const latest = await request('/api/iq/review/' + job.id);
        await pollReview(panel, latest, settled);
        // 只在在途任务转为终态时刷新成绩，展示已有历史结论不能造成轮询递归。
        if (latest.status !== 'running') { state.detailKey = ''; await refresh(); }
      }
      catch (e) { panel.append(element('p', e.message, 'iq-error')); settled(null); }
    }, 1800);
  }

  /**
   * 添加显式付费复核入口，网络重试复用请求 ID，避免重复收费。
   * @param {HTMLElement} item 当前题目条目
   * @param {string} runId 原批次 ID
   * @param {Object} result 原结果身份
   * @param {Object|undefined} previous 最新历史建议
   */
  function appendReview(item, runId, result, previous) {
    const label = element('span', 'Agent 复核');
    const button = element('button'); button.type = 'button'; button.append(label);
    const icon = window.BenchUI?.icon('search'); if (icon) { button.prepend(icon); }
    const panel = element('div', undefined, 'iq-review-result');
    item.append(button, panel);
    if (result.final?.score === 1) {
      button.remove();
      if (previous) { renderReview(panel, previous); }
      return;
    }
    let pending = null;
    /**
     * 已结束任务允许显式再试；网络未知状态仍保留原 ID，不能因轮询失败重复扣费。
     * @param {Object|null} job 终态任务，null 表示网络状态未知
     */
    function settled(job) {
      button.disabled = false;
      if (job) { pending = null; }
      label.textContent = job?.status === 'completed' ? '重新复核' : '重试复核';
    }
    if (previous) {
      const body = { run_id: runId, site: result.site, model: result.model, question_id: result.question_id,
        repeat: result.repeat, judge_site: previous.judge_site, judge_model: previous.judge_model };
      pending = { signature: JSON.stringify(body), id: previous.id };
      button.disabled = previous.status === 'running';
      pollReview(panel, previous, settled);
    }
    button.addEventListener('click', async () => {
      const judgeSite = node('iq-judge-site').value;
      const judgeModel = node('iq-judge-model').value;
      if (!judgeSite || !judgeModel) {
        node('iq-review-settings').open = true;
        node('iq-review-error').textContent = '请先选择复核站点和模型'; node('iq-review-error').hidden = false;
        return;
      }
      const body = { run_id: runId, site: result.site, model: result.model, question_id: result.question_id,
        repeat: result.repeat, judge_site: judgeSite, judge_model: judgeModel };
      const signature = JSON.stringify(body);
      if (!pending || pending.signature !== signature) { pending = { signature, id: crypto.randomUUID().replaceAll('-', '') }; }
      button.disabled = true;
      try {
        const job = await request('/api/iq/review', { ...body, request_id: pending.id });
        await pollReview(panel, job, settled);
      } catch (e) { panel.replaceChildren(element('p', e.message, 'iq-error')); settled(null); }
    });
  }

  /** 同步选择中的批次；只维持一个轮询，隐藏页面时停止主动刷新。 */
  async function refresh() {
    const ticket = ++state.runRequest;
    clearTimeout(state.timer);
    try {
      await histories();
      if (ticket !== state.runRequest) { return; }
      const id = node('iq-history').value;
      if (id) {
        const run = await request('/api/iq/run/' + id);
        if (ticket !== state.runRequest) { return; }
        render(run); await details();
      }
      else { node('iq-summary').replaceChildren(element('div', '暂无评测成绩', 'iq-empty')); }
      await loadRanking();
    } catch (e) {
      error(e.message);
      if (!state.retrySubmitting && state.run?.retry && ['running', 'stopping'].includes(state.run.status)) {
        state.retryNotice = { runId: state.run.run_id, kind: 'poll-error',
          title: '重试已启动，进度暂时无法更新', description: e.message + '。正在重新读取进度，不会重复提交。' };
        retryFeedback();
      }
    }
    finally { if (state.visible && (state.busy || state.reviewBusy)) { state.timer = setTimeout(refresh, 1800); } }
  }

  /**
   * 打开排行榜来源或历史记录，阻止先前发出的轮询覆盖用户新选择。
   * @param {string} runId 已存在的批次标识
   * @param {Array<string>|null} pair 可选站点与模型；排行榜入口会同时打开逐题记录
   * @return {Promise<void>} 记录和可选详情已显示
   */
  async function showRun(runId, pair = null) {
    const ticket = ++state.runRequest;
    const run = await request('/api/iq/run/' + runId);
    if (ticket !== state.runRequest) { return; }
    state.detailPair = pair; state.detailKey = ''; node('iq-details').replaceChildren();
    render(run); await histories(); await details();
    if (state.visible && (state.busy || state.reviewBusy)) {
      clearTimeout(state.timer); state.timer = setTimeout(refresh, 1800);
    }
    window.BenchUI?.showPanel('iq', 'records', true);
  }

  /** 创建批次，连接失败后保留同一个请求 ID，用户重试不会重复消耗额度。 */
  async function start() {
    if (state.submitting || state.retrySubmitting || state.busy || !state.picked.size) { return; }
    for (const input of node('iq-fields').querySelectorAll('input[type=number]')) { if (!input.reportValidity()) { return; } }
    const body = { suite: node('iq-suite').value, repeats: Number(node('iq-repeats').value), max_tokens: Number(node('iq-tokens').value),
      thinking_mode: node('iq-thinking').value,
      timeout_seconds: Number(node('iq-timeout').value), concurrency: Number(node('iq-concurrency').value),
      tasks: Array.from(state.picked).map(key => { const pair = JSON.parse(key); return { site: pair[0], model: pair[1] }; }) };
    const signature = JSON.stringify(body);
    if (!state.pending || state.pending.signature !== signature) { state.pending = { signature, id: crypto.randomUUID().replaceAll('-', '') }; }
    body.request_id = state.pending.id;
    state.submitting = true; plan(); error('');
    try {
      const run = await request('/api/iq/run', body);
      state.pending = null; state.run = run; state.detailPair = null; state.detailKey = ''; node('iq-details').replaceChildren();
      window.BenchUI?.showPanel('iq', 'records', true);
      await refresh();
    } catch (e) { error(e.message); }
    finally { state.submitting = false; plan(); }
  }

  /** 点击后立即展示页面内确认；不依赖嵌入式浏览器是否允许原生 confirm 对话框。 */
  function retryFailed() {
    const source = state.run;
    if (!source?.retryable_errors || state.busy || state.submitting || state.retrySubmitting ||
        ['running', 'stopping'].includes(source.status)) { return; }
    state.retryConfirmation = { runId: source.run_id }; state.retryNotice = null;
    retryFeedback();
    node('iq-retry-confirm').scrollIntoView({ block: 'nearest' });
    node('iq-retry-confirm-submit').focus({ preventScroll: true });
  }

  /**
   * 确认后只补失败项，立即显示提交状态；网络结果不明时复用请求 ID，避免重复收费。
   * POST 返回后先展示快照，即使后续状态查询失败，也不能把已启动的任务说成提交失败。
   * @return {Promise<void>} 提交和状态同步完成，或错误已呈现在当前记录旁
   */
  async function submitRetry() {
    const source = state.run;
    if (state.retryConfirmation?.runId !== source?.run_id || !source?.retryable_errors ||
        state.busy || state.submitting || state.retrySubmitting || ['running', 'stopping'].includes(source.status)) { return; }
    state.retryConfirmation = null;
    if (!state.retryPending || state.retryPending.source !== source.run_id) {
      state.retryPending = { source: source.run_id, id: crypto.randomUUID().replaceAll('-', '') };
    }
    state.retryNotice = { runId: source.run_id, kind: 'submitting', title: '正在提交原地重试…',
      description: '正在提交 ' + source.retryable_errors + ' 项接口失败，请稍候。' };
    state.retrySubmitting = true; state.submitting = true; plan(); error('');
    let accepted = false;
    try {
      const run = await request('/api/iq/retry', { run_id: source.run_id, request_id: state.retryPending.id });
      accepted = true; state.retryPending = null; state.retryNotice = null; state.retrySubmitting = false;
      // 直接显示提交响应，不再等第二次 GET；在途轮询必须失效，不能用旧完成状态覆盖它。
      ++state.runRequest; clearTimeout(state.timer);
      state.active = ['running', 'stopping'].includes(run.status) ? run : null; state.busy = Boolean(state.active);
      state.detailKey = ''; render(run);
      window.BenchUI?.showPanel('iq', 'records', true);
      await refresh();
    } catch (e) {
      state.retryNotice = { runId: source.run_id, kind: 'error',
        title: accepted ? '重试已提交，进度暂时无法读取' : '未能确认重试是否提交',
        description: e.message + (accepted ? '。刷新运行记录查看进度。' : '。可重新确认重试，将沿用同一请求标识，避免重复提交。') };
      error(e.message);
    } finally { state.retrySubmitting = false; state.submitting = false; plan(); }
  }

  /** 取消页面内确认只关闭提示，不停止后台任务，也不发送收费请求。 */
  function cancelRetryConfirmation() {
    state.retryConfirmation = null;
    state.retryNotice = { runId: state.run?.run_id, kind: 'cancelled', title: '已取消本次重试', description: '没有提交新的请求。' };
    retryFeedback(); node('iq-retry').focus({ preventScroll: true });
  }

  /** 初始化仅获取本机保存的元数据；不因进入页面自动发送付费请求。 */
  async function init() {
    if (state.ready) { await refresh(); return; }
    if (state.loading) { return; }
    state.loading = true;
    try {
      const [saved, catalog, gallery] = await Promise.all([request('/api/config'), request('/api/iq/suites'), request('/api/gallery')]);
      state.sites = saved.sites; state.suites = catalog.suites;
      state.judgeSite = saved.iq_auto_review_site || ''; state.judgeModel = saved.iq_auto_review_model || '';
      // 刷新运行记录或返回本页时保留题集选择，避免把完整评测静默改成试跑。
      rankOptions(node('iq-suite'), state.suites.map(s => [s.id, (s.name || s.id) + ' · ' + s.count + ' 题']),
        node('iq-suite').value || 'bbeh-smoke', '无可用题集');
      // 兼容只有旧题集的安装或测试环境。正常安装默认显示 BBEH 正式榜。
      if (!state.suites.some(s => s.benchmark)) { node('iq-rank-benchmark').value = 'foundation'; }
      state.sites.forEach(site => {
        state.models[site.name] = Array.from(new Set((site.models || []).concat((gallery.cells || []).filter(c => c.site === site.name).map(c => c.model)))).sort();
      });
      state.ready = true; renderModels(); judgeOptions(); await refresh();
    } catch (e) { error(e.message); }
    finally { state.loading = false; plan(); }
  }

  /** 登录状态变更只刷新官方目录，不丢弃其他渠道选项或发起评测。 */
  function refreshOfficialAccount(event) {
    if (!state.ready) { return; }
    const account = event.detail;
    const site = state.sites.find(site => site.builtin && site.name === account.name);
    if (!site) { return; }
    Object.assign(site, account);
    state.models[site.name] = site.ready ? site.models || [] : [];
    renderModels(); judgeOptions();
  }
  // 等共享账号模块强制刷新完目录后再读取，避免命中授权前的 30 秒缓存。
  document.addEventListener('chatgpt-models-changed', refreshOfficialAccount);
  document.addEventListener('kimi-models-changed', refreshOfficialAccount);
  node('iq-load-models').addEventListener('click', loadModels);
  node('iq-select-all').addEventListener('click', () => selectModels(node('iq-models'), true));
  node('iq-select-none').addEventListener('click', () => selectModels(node('iq-models'), false));
  node('iq-rank-refresh').addEventListener('click', loadRanking);
  node('iq-rank-sort').addEventListener('change', () => { state.rankPage = 1; renderRanking(); });
  node('iq-judge-site').addEventListener('change', judgeChanged);
  node('iq-judge-model').addEventListener('change', judgeChanged);
  node('iq-judge-save').addEventListener('click', saveJudgeSettings);
  node('iq-judge-refresh').addEventListener('click', async () => {
    const site = node('iq-judge-site').value;
    if (!site || state.judgeSaving || state.judgeLoading) { return; }
    state.judgeLoading = true; judgeSettingsState();
    try {
      const data = await request('/api/models?site=' + encodeURIComponent(site));
      if (data.error) { throw new Error(data.error); }
      state.models[site] = data.models || []; judgeOptions(); node('iq-review-error').hidden = true;
    } catch (e) { node('iq-review-error').textContent = e.message; node('iq-review-error').hidden = false; }
    finally { state.judgeLoading = false; judgeSettingsState(); }
  });
  node('iq-rank-cohort').addEventListener('change', () => { state.rankPage = 1; loadRanking(); });
  node('iq-rank-benchmark').addEventListener('change', () => {
    node('iq-rank-cohort').replaceChildren(); state.rankPage = 1; loadRanking();
  });
  node('iq-rank-prev').addEventListener('click', () => rankPage(state.rankPage - 1));
  node('iq-rank-next').addEventListener('click', () => rankPage(state.rankPage + 1));
  node('iq-rank-page').addEventListener('change', event => rankPage(event.target.value));
  node('iq-rank-page-size').addEventListener('change', event => {
    const size = Number(event.target.value);
    if (![10, 20, 50].includes(size)) { return; }
    state.rankPageSize = size; state.rankPage = 1; renderRanking();
  });
  node('iq-rank-modes').addEventListener('change', event => {
    state.rankMode = event.target.value; state.rankPage = 1; renderRanking();
  });
  node('iq-rank-scope').addEventListener('change', () => {
    state.rankScope[state.rankMode] = node('iq-rank-scope').value; state.rankPage = 1; renderRanking();
  });
  node('iq-fields').addEventListener('change', plan);
  node('iq-start').addEventListener('click', start);
  ['bars', 'table'].forEach(view => node('iq-view-' + view).addEventListener('click', () => {
    state.rankView = view;
    // 只切换呈现；保留当前页和筛选，也不重新请求或启动任何评测。
    renderRanking();
  }));
  node('iq-retry').addEventListener('click', retryFailed);
  node('iq-retry-confirm-submit').addEventListener('click', submitRetry);
  node('iq-retry-confirm-cancel').addEventListener('click', cancelRetryConfirmation);
  node('iq-retry-source').addEventListener('click', () => {
    if (state.run?.retry) { showRun(state.run.retry.source_run_id).catch(e => error(e.message)); }
  });
  node('iq-refresh').addEventListener('click', () => { error(''); init(); });
  node('iq-history').addEventListener('change', async () => {
    try { await showRun(node('iq-history').value); } catch (e) { error(e.message); }
  });
  node('iq-active-run').addEventListener('click', () => {
    if (state.active) { showRun(state.active.run_id).catch(e => error(e.message)); }
  });
  node('iq-stop').addEventListener('click', async () => {
    if (!state.run) { return; }
    node('iq-stop').disabled = true;
    try { render(await request('/api/iq/cancel', { run_id: state.run.run_id })); await refresh(); } catch (e) { error(e.message); node('iq-stop').disabled = false; }
  });
  node('iq-export').addEventListener('click', async () => {
    if (!state.run) { return; }
    try {
      const data = await request('/api/iq/run/' + state.run.run_id + '?full=1');
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
      const link = element('a'); link.href = url; link.download = 'iq-' + data.run_id + '.json'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { error(e.message); }
  });
  document.addEventListener('view-change', event => {
    state.visible = event.detail === 'iq';
    if (state.visible) { init(); }
    else { clearTimeout(state.timer); }
  });
}());
