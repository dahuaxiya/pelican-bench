/** Site history uses server-persisted probes; no timer in this page sends model requests. */
(function () {
  'use strict';
  const $ = name => document.getElementById('availability-' + name);
  if (!$('list')) { return; }
  const labels = { success: '正常', failure: '异常', mixed: '部分失败', unknown: '未知', running: '检测中', stale: '已过期' };
  let hours = 24, visible = false, timer, clockTimer, snapshot, snapshotReceivedAt = 0, loading = false, submitting = false;
  let draft = false, settingsKey = '', historyQuery = null, historySequence = 0;
  let tooltipAnchor = null, tooltipTimer;
  const rows = new Map();

  /** Create text-only DOM; site names, model names and diagnostics never become HTML. */
  function node(tag, text, className) {
    const value = document.createElement(tag);
    if (text != null) { value.textContent = text; }
    if (className) { value.className = className; }
    return value;
  }

  /** Read bounded local JSON and surface failed reads without clearing the last good view. */
  async function request(path, body) {
    const response = await fetch('/api/availability' + path, body === undefined ? { cache: 'no-store' } : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    const value = await response.json();
    if (!response.ok) { throw new Error(value.error || '读取监测记录失败'); }
    return value;
  }

  /** Render a recoverable error beside the monitor controls. */
  function error(message = '') { $('error').textContent = message; $('error').hidden = !message; }

  /** Format actual sample times in the viewer's local timezone. */
  function time(value) { return new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }); }

  /** Show bucket boundaries compactly while keeping full times on the buttons. */
  function shortTime(value) {
    return new Date(value * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  }

  /** A mixed bucket stays visibly degraded; unknown or missing samples cannot turn it green. */
  function bucketState(bucket) {
    if (bucket.failure) { return bucket.success ? 'mixed' : 'failure'; }
    if (bucket.success) { return 'success'; }
    if (bucket.running) { return 'running'; }
    return 'unknown';
  }

  /**
   * Format recorded total duration, leaving missing values unknown rather than zero.
   * @param {number|null} seconds Stored elapsed time including discovery and the full response.
   * @return {string} Milliseconds for display; this is not first-token latency or ping.
   */
  function duration(seconds) {
    return Number.isFinite(seconds) ? Math.round(seconds * 1000).toLocaleString('zh-CN') + ' ms' : '—';
  }

  /**
   * Describe one request's usage without presenting estimates as billing data.
   * @param {object|undefined} usage Sanitized counters supplied by the local service.
   * @return {string} Reported or estimated tokens, with missing values explicit.
   */
  function usageText(usage) {
    if (!usage || usage.source === 'missing' || !Number.isFinite(usage.total_tokens)) { return '用量未知'; }
    const parts = [(usage.source === 'reported' ? '接口返回 ' : '估算 ') + usage.total_tokens.toLocaleString('zh-CN') + ' token'];
    for (const [key, label] of [['prompt_tokens', '输入'], ['completion_tokens', '输出'], ['reasoning_tokens', '其中推理'], ['cached_tokens', '输入中缓存']]) {
      if (Number.isFinite(usage[key])) { parts.push(label + ' ' + usage[key].toLocaleString('zh-CN')); }
    }
    return parts.join(' · ');
  }

  /**
   * Render today's reported subtotal and its coverage independently of history filters.
   * @param {object} row Mounted card nodes, retained during polling.
   * @param {object|undefined} daily Local-day counters; older servers can omit them.
   */
  function updateUsage(row, daily) {
    row.tokens.textContent = Number.isFinite(daily?.total_tokens) ? daily.total_tokens.toLocaleString('zh-CN') : '—';
    if (!daily) { row.usageNote.textContent = '用量未知'; return; }
    if (!daily.requests) { row.usageNote.textContent = '今日尚未检测'; return; }
    const notes = ['已统计 ' + daily.reported_requests + ' / ' + daily.requests + ' 次'];
    if (daily.estimated_requests) { notes.push(daily.estimated_requests + ' 次仅有估算'); }
    if (daily.missing_requests) { notes.push(daily.missing_requests + ' 次用量未知'); }
    if (daily.running_requests) { notes.push(daily.running_requests + ' 次检测中'); }
    row.usageNote.textContent = notes.join(' · ');
    row.usage.title = '按服务所在电脑的自然日累计，始于 ' + time(daily.start) + '；只累加接口返回的 token，不代表费用。' +
      (daily.estimated_requests ? ' 另有估算 ' + daily.estimated_tokens.toLocaleString('zh-CN') + ' token，未计入。' : '');
  }

  /** Dismiss the preview and detach its accessible description from the old time point. */
  function hideTooltip() {
    clearTimeout(tooltipTimer);
    tooltipAnchor?.removeAttribute('aria-describedby');
    tooltipAnchor = null;
    $('tooltip').hidden = true;
  }

  /** Allow the pointer to reach the preview without losing it in the small gap. */
  function deferTooltipClose() {
    clearTimeout(tooltipTimer);
    tooltipTimer = setTimeout(hideTooltip, 120);
  }

  /**
   * Show already-loaded history, so moving across bars never sends more requests.
   * @param {HTMLButtonElement} anchor Focused or hovered historical interval.
   * @param {object} bucket Frozen interval counts and its last actual sample.
   */
  function showTooltip(anchor, bucket) {
    hideTooltip();
    tooltipAnchor = anchor;
    anchor.setAttribute('aria-describedby', 'availability-tooltip');
    const tip = $('tooltip'), sample = bucket.latest;
    // The timestamp and badge describe the same actual attempt. Bucket counts
    // below explain mixed intervals without giving an unknown last attempt a green badge.
    const state = sample?.status || bucketState(bucket);
    const badge = node('span', state === 'unknown' && !sample ? '未检测' : labels[state], 'availability-state');
    badge.dataset.state = state;
    const heading = node('div', null, 'availability-tooltip-heading');
    heading.append(badge, node('time', sample ? time(sample.started_at) : shortTime(bucket.start)));
    const details = node('dl', null, 'availability-tooltip-metrics');
    const counts = bucket.success + ' 成功 / ' + bucket.failure + ' 失败' +
      (bucket.unknown ? ' / ' + bucket.unknown + ' 未知' : '') + (bucket.running ? ' / ' + bucket.running + ' 检测中' : '');
    details.append(node('dt', '最近检测耗时'), node('dd', duration(sample?.duration_seconds)),
      node('dt', '该时段记录'), node('dd', counts));
    if (sample) { details.append(node('dt', '本次用量'), node('dd', usageText(sample.usage))); }
    const count = bucket.success + bucket.failure + bucket.unknown + bucket.running;
    const note = node('p', sample ? '最近一次：' + (sample.reason || labels[sample.status]) : count
      ? '该时段有 ' + count + ' 次检测，完整记录可展开查看。' : '这段时间没有检测记录。', 'availability-tooltip-note');
    const range = node('small', shortTime(bucket.start) + ' ~ ' + shortTime(bucket.end), 'availability-tooltip-range');
    tip.replaceChildren(heading, details, note, range);
    tip.hidden = false;
    // Fixed positioning avoids card clipping. Flip below a point near the top and
    // clamp both axes so previews remain readable on small screens and at page edges.
    const rect = anchor.getBoundingClientRect(), bounds = tip.getBoundingClientRect();
    const x = Math.max(12, Math.min(rect.left + rect.width / 2 - bounds.width / 2, window.innerWidth - bounds.width - 12));
    const above = rect.top - bounds.height - 12;
    const y = above >= 12 ? above : Math.max(12, Math.min(rect.bottom + 12, window.innerHeight - bounds.height - 12));
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }

  /** Update countdown text locally; the service remains the only owner of probe scheduling. */
  function updateCountdowns() {
    if (!snapshot) { return; }
    const now = snapshot.now + (performance.now() - snapshotReceivedAt) / 1000;
    const remaining = Math.max(0, Math.ceil((snapshot.next_at || now) - now));
    for (const row of rows.values()) {
      row.countdown.textContent = row.state === 'running' ? '正在检测' : !snapshot.settings.enabled || !row.enabled
        ? '自动检测已暂停' : remaining ? '下次更新 ' + Math.floor(remaining / 60) + '分 ' + String(remaining % 60).padStart(2, '0') + '秒' : '等待检测';
    }
  }

  /**
   * Update a site card without replacing interactive elements during polling.
   * @param {object} item Public site state, counts and 60 historical intervals.
   * @param {object} data Snapshot supplying the selected window and service clock.
   */
  function siteRow(item, data) {
    let row = rows.get(item.site);
    if (!row) {
      const root = node('article', null, 'availability-site');
      const heading = node('div', null, 'availability-site-heading');
      const emblem = node('span', null, 'availability-site-icon');
      const symbol = window.BenchUI?.icon('server'); if (symbol) { emblem.append(symbol); }
      emblem.setAttribute('aria-hidden', 'true');
      const title = node('button', item.site, 'availability-site-name');
      title.type = 'button'; title.title = '查看该站点的检测记录';
      title.addEventListener('click', () => openHistory(item.site, snapshot.start, snapshot.now + .001));
      const model = node('span', null, 'availability-site-model');
      const identity = node('div', null, 'availability-identity'); identity.append(title, model);
      const badge = node('span', null, 'availability-state');
      heading.append(emblem, identity, badge);
      const metrics = node('dl', null, 'availability-metrics');
      const latency = node('strong', null, 'availability-latency');
      const score = node('strong', null, 'availability-score');
      const latencyLabel = node('dt', '检测耗时');
      latencyLabel.title = '一次检测的总耗时，含模型发现及完整响应；不是首字延迟或 PING';
      const rateLabel = node('dt', '可用率');
      rateLabel.title = '所选时间内成功检测次数 / 有明确结果的检测次数；未知不计入';
      const latencyIcon = window.BenchUI?.icon('zap'), rateIcon = window.BenchUI?.icon('activity');
      if (latencyIcon) { latencyLabel.prepend(latencyIcon); }
      if (rateIcon) { rateLabel.prepend(rateIcon); }
      const latencyValue = node('dd'), rateValue = node('dd'); latencyValue.append(latency); rateValue.append(score);
      const latencyGroup = node('div'), rateGroup = node('div');
      latencyGroup.append(latencyLabel, latencyValue); rateGroup.append(rateLabel, rateValue);
      metrics.append(latencyGroup, rateGroup);
      const count = node('span', null, 'availability-count');
      const usage = node('div', null, 'availability-usage');
      const tokenHeading = node('div');
      const tokens = node('strong', null, 'availability-tokens');
      tokenHeading.append(node('span', '今日 token'), tokens);
      const usageNote = node('small', null, 'availability-usage-note');
      usage.append(tokenHeading, usageNote);
      const historyHeading = node('div', null, 'availability-history-heading');
      const countdown = node('span');
      const schedule = node('span', null, 'availability-countdown');
      const clock = window.BenchUI?.icon('clock-3'); if (clock) { schedule.append(clock); } schedule.append(countdown);
      historyHeading.append(node('span', '历史状态'), schedule);
      const bars = node('div', null, 'availability-bars'); bars.setAttribute('aria-label', item.site + ' 历史状态');
      const times = node('div', null, 'availability-axis');
      const start = node('span'), end = node('span'); times.append(start, end);
      const latest = node('p', null, 'availability-latest');
      root.append(heading, metrics, count, usage, historyHeading, bars, times, latest);
      // Keep all bars mounted. Replacing a hovered button every poll causes the
      // preview to blink and can change which time interval receives the click.
      const points = Array.from({ length: 60 }, () => {
        const button = node('button'); button.type = 'button';
        const point = { button, bucket: null };
        button.addEventListener('pointerenter', event => { if (event.pointerType !== 'touch') { showTooltip(button, point.bucket); } });
        button.addEventListener('pointerleave', () => { if (document.activeElement !== button) { deferTooltipClose(); } });
        button.addEventListener('focus', () => showTooltip(button, point.bucket));
        button.addEventListener('blur', hideTooltip);
        button.addEventListener('click', () => openHistory(item.site, point.bucket.start, point.bucket.end));
        bars.append(button); return point;
      });
      row = { root, badge, score, latency, model, count, usage, tokens, usageNote, bars, points, start, end, latest, countdown };
      rows.set(item.site, row); $('list').append(root);
    }
    row.enabled = item.enabled; row.state = item.state;
    row.badge.dataset.state = item.state;
    row.badge.textContent = labels[item.state] || '未知';
    row.model.textContent = item.latest?.model || item.model || '尚未选择探测模型';
    row.model.title = '探测模型：' + row.model.textContent;
    row.latency.textContent = duration(item.latest?.duration_seconds);
    row.score.textContent = item.availability == null ? '暂无记录' : item.availability.toFixed(2) + '%';
    row.score.dataset.state = item.availability == null ? 'unknown' : item.availability === 100 ? 'success' : item.availability === 0 ? 'failure' : 'mixed';
    row.count.textContent = item.total ? item.success + ' / ' + item.total + ' 次成功' : '尚无有效检测';
    if (item.unknown) { row.count.textContent += ' · ' + item.unknown + ' 次未知'; }
    updateUsage(row, item.today_usage);
    row.start.textContent = shortTime(data.start); row.end.textContent = '现在';
    const sample = item.latest;
    row.latest.textContent = sample
      ? '最近检测 ' + time(sample.started_at) + ' · ' + (sample.reason || labels[sample.status])
      : '尚未检测';
    // Never replace a focused history point on a timer. Keyboard users must be
    // able to inspect a time interval without focus jumping back to the page.
    if (!row.bars.contains(document.activeElement) && !row.bars.contains(tooltipAnchor)) {
      item.buckets.forEach((bucket, index) => {
        const point = row.points[index], button = point.button;
        point.bucket = bucket;
        button.dataset.state = bucketState(bucket);
        const total = bucket.success + bucket.failure;
        const description = time(bucket.start) + ' ~ ' + time(bucket.end) + '：' +
          (total ? bucket.success + ' 次成功，' + bucket.failure + ' 次失败' : bucket.running ? '检测中' : '无有效检测') +
          (bucket.unknown ? '，' + bucket.unknown + ' 次未知' : '');
        button.setAttribute('aria-label', description);
      });
    }
  }

  /** Refresh server-selected models only when the user has no unsaved settings. */
  function renderSettings(settings) {
    const key = JSON.stringify(settings.sites);
    if (draft || key === settingsKey) { return; }
    settingsKey = key;
    $('site-settings').replaceChildren();
    for (const site of settings.sites) {
      const entry = node('div', null, 'availability-setting'); entry.dataset.site = site.site;
      const label = node('label', null, 'inline');
      const enabled = node('input'); enabled.type = 'checkbox'; enabled.checked = site.enabled;
      label.append(enabled, node('span', site.site));
      const model = node('input'); model.value = site.model; model.placeholder = '自动选择探测模型';
      model.maxLength = 256; model.setAttribute('aria-label', site.site + ' 探测模型');
      entry.append(label, model); $('site-settings').append(entry);
    }
    $('settings-count').textContent = settings.sites.filter(site => site.enabled).length + ' 个站点';
  }

  /**
   * Render real server state without resetting the window selector or open details.
   * @param {object} data Successful snapshot, with its server time as the countdown baseline.
   */
  function render(data) {
    snapshot = data;
    snapshotReceivedAt = performance.now();
    $('up').textContent = data.rows.filter(row => row.state === 'success').length;
    $('down').textContent = data.rows.filter(row => row.state === 'failure').length;
    $('unknown').textContent = data.rows.filter(row => !['success', 'failure'].includes(row.state)).length;
    $('enabled').checked = data.settings.enabled;
    $('enabled').disabled = submitting;
    $('check').disabled = submitting;
    $('check').lastChild.textContent = '刷新状态';
    $('progress').textContent = data.busy ? '正在检测 ' + data.active_count + ' 个站点' : data.settings.enabled
      ? '每 5 分钟 · 下次 ' + time(data.next_at) : '自动监测已暂停';
    if (data.error) { error(data.error); }
    const names = new Set(data.rows.map(row => row.site));
    for (const [name, row] of rows) { if (!names.has(name)) { row.root.remove(); rows.delete(name); } }
    for (const row of data.rows) { siteRow(row, data); }
    updateCountdowns();
    $('empty').hidden = !!data.rows.length;
    renderSettings(data.settings);
  }

  /** Poll only while visible; serial requests avoid stale responses replacing a new time range. */
  async function refresh() {
    if (loading || !visible) { return; }
    loading = true;
    const requestedHours = hours;
    try {
      const data = await request('?hours=' + requestedHours);
      if (hours === requestedHours) { render(data); }
    } catch (e) { error(e.message); }
    finally {
      loading = false; clearTimeout(timer);
      if (visible) { timer = setTimeout(refresh, hours !== requestedHours ? 0 : snapshot?.busy ? 1500 : 15000); }
    }
  }

  /** Collect only site identifiers and probe models; credentials stay in server configuration. */
  function settings(enabled) {
    return { enabled, sites: Array.from($('site-settings').children, entry => ({
      site: entry.dataset.site, enabled: entry.querySelector('input[type=checkbox]').checked,
      model: entry.querySelector('input:not([type=checkbox])').value.trim()
    })) };
  }

  /** Save the explicit switch and draft together, with immediate progress and failure feedback. */
  async function save(enabled) {
    if (submitting) { return; }
    submitting = true; error(); $('enabled').disabled = true; $('check').disabled = true;
    $('saved').textContent = '保存中…';
    try {
      const data = settings(enabled);
      // On first enable, the existing site's checkboxes are the scope selector.
      // An empty selection is surfaced inline, never silently broadened to all sites.
      await request('/settings', data);
      draft = false; settingsKey = '';
      $('saved').textContent = enabled ? '已保存，自动监测已开启' : '已保存';
    } catch (e) { error(e.message); $('settings').open = true; $('saved').textContent = ''; }
    finally { submitting = false; await refresh(); }
  }

  /**
   * Freeze a clicked time range and dismiss its preview before opening full details.
   * @param {string} site Exact configured or historical site name.
   * @param {number} start Inclusive interval start, in Unix seconds.
   * @param {number} end Exclusive interval end, in Unix seconds.
   */
  function openHistory(site, start, end) {
    hideTooltip();
    historyQuery = { site, start, end, page: 1 };
    $('detail-title').textContent = site;
    $('detail-range').textContent = time(start) + ' ~ ' + time(end);
    if (!$('detail').open) { $('detail').showModal(); }
    loadHistory();
  }

  /** Page historical attempts; discard responses for a dialog selection that has since changed. */
  async function loadHistory() {
    const sequence = ++historySequence;
    $('detail-records').replaceChildren(node('p', '读取中…', 'muted'));
    $('prev').disabled = $('next').disabled = true;
    try {
      const data = await request('/history?' + new URLSearchParams(historyQuery));
      if (sequence !== historySequence) { return; }
      const records = data.rows.map(item => {
        const record = node('article', null, 'availability-record');
        const heading = node('div', null, 'availability-record-heading');
        const state = node('span', labels[item.status], 'availability-state'); state.dataset.state = item.status;
        heading.append(node('time', time(item.started_at)), state);
        record.append(heading, node('p', item.reason || '检测中'), node('small',
          (item.model || '未选定模型') + (item.duration_seconds == null ? '' : ' · ' + item.duration_seconds.toFixed(2) + ' 秒')));
        record.append(node('p', usageText(item.usage), 'availability-record-usage'));
        return record;
      });
      $('detail-records').replaceChildren(...(records.length ? records : [node('p', '这段时间没有检测记录。', 'empty-state')]));
      const pages = Math.max(1, Math.ceil(data.total / data.page_size));
      $('detail-page').textContent = '第 ' + data.page + ' / ' + pages + ' 页 · ' + data.total + ' 次';
      $('prev').disabled = data.page <= 1; $('next').disabled = data.page >= pages;
    } catch (e) {
      if (sequence === historySequence) { $('detail-records').replaceChildren(node('p', e.message, 'run-error')); }
    }
  }

  $('enabled').addEventListener('change', () => save($('enabled').checked));
  $('form').addEventListener('input', () => { draft = true; $('saved').textContent = '有未保存的修改'; });
  $('form').addEventListener('submit', event => { event.preventDefault(); save($('enabled').checked); });
  $('check').addEventListener('click', async () => {
    if (loading || submitting) { return; }
    submitting = true; $('check').disabled = true; $('progress').textContent = '正在刷新状态…'; error();
    // Refresh reads saved outcomes only. Five-minute scheduling belongs to the
    // service, so repeated clicks or multiple tabs cannot consume extra requests.
    try { await refresh(); }
    finally { submitting = false; $('check').disabled = false; }
  });
  document.querySelectorAll('[data-availability-hours]').forEach(button => button.addEventListener('click', () => {
    hideTooltip();
    hours = Number(button.dataset.availabilityHours);
    document.querySelectorAll('[data-availability-hours]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    refresh();
  }));
  $('detail-close').addEventListener('click', () => $('detail').close());
  $('detail').addEventListener('close', () => { historySequence++; });
  $('prev').addEventListener('click', () => { historyQuery.page--; loadHistory(); });
  $('next').addEventListener('click', () => { historyQuery.page++; loadHistory(); });
  $('tooltip').addEventListener('pointerenter', () => clearTimeout(tooltipTimer));
  $('tooltip').addEventListener('pointerleave', deferTooltipClose);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') { hideTooltip(); } });
  window.addEventListener('scroll', hideTooltip, { passive: true, capture: true });
  window.addEventListener('resize', hideTooltip);
  document.addEventListener('view-change', event => {
    visible = event.detail === 'availability'; clearTimeout(timer); clearInterval(clockTimer); hideTooltip();
    if (visible) { refresh(); clockTimer = setInterval(updateCountdowns, 1000); }
  });
}());
