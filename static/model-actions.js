/** 首页和在线报告共用的模型操作。所有远端文本用 textContent 写入。 */
window.ModelActions = (function () {
  'use strict';

  /**
   * 创建元素；调用方只能传文本，不能传 HTML。
   * @param {string} tag 标签名
   * @param {string} text 可选文本
   * @param {string} className 可选样式类
   * @return {HTMLElement} 新节点
   */
  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text) { result.textContent = text; }
    if (className) { result.className = className; }
    return result;
  }

  /**
   * 请求本地 API；HTTP 错误保留服务端的说明。
   * @param {string} url 本地相对地址
   * @param {Object} options 可选 fetch 参数
   * @return {Promise<Object>} JSON 结果
   */
  async function request(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) { throw new Error(data.error || ('HTTP ' + response.status)); }
    return data;
  }

  /**
   * 生成精确组合的查询参数，名称中的斜杠等字符不能参与路径拼接。
   * @param {string} site 站点名
   * @param {string} model 模型名
   * @return {URLSearchParams} 已编码的参数
   */
  function query(site, model) { return new URLSearchParams({ site: site, model: model }); }

  /**
   * 打开独立的历史对话框；关闭或切版本后忽略晚到的预览响应。
   * @param {string} site 站点名
   * @param {string} model 模型名
   * @return {Promise<void>} 列表加载结束，不等待用户关闭
   */
  async function history(site, model) {
    const dialog = node('dialog', '', 'model-history');
    dialog.setAttribute('aria-label', site + ' / ' + model + ' 历史版本');
    const head = node('div', '', 'model-history-head');
    head.appendChild(node('h2', model + ' · 历史版本'));
    const close = node('button', '关闭');
    close.type = 'button';
    close.addEventListener('click', function () { dialog.close(); });
    head.appendChild(close);
    dialog.appendChild(head);
    dialog.appendChild(node('p', site + ' · 按运行批次从新到旧排列', 'model-hint'));
    const info = node('p', '正在读取…', 'model-hint');
    info.setAttribute('role', 'status');
    dialog.appendChild(info);
    const select = node('select');
    select.setAttribute('aria-label', '选择历史版本');
    select.hidden = true;
    dialog.appendChild(select);
    const preview = node('div', '', 'model-history-preview');
    preview.hidden = true;
    const root = preview.attachShadow({ mode: 'open' });
    dialog.appendChild(preview);
    const detail = node('p', '', 'model-version-detail');
    dialog.appendChild(detail);
    const report = node('a', '打开这次报告', 'model-report-link');
    report.target = '_blank';
    report.rel = 'noopener';
    report.hidden = true;
    dialog.appendChild(report);
    let versionRequest = 0;
    dialog.addEventListener('close', function () {
      // 对话框移除后不再写 DOM，避免关闭后请求返回造成预览错位。
      versionRequest += 1;
      dialog.remove();
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    try {
      const data = await request('/api/gallery/history?' + query(site, model));
      if (!dialog.open) { return; }
      const versions = data.versions || [];
      info.textContent = versions.length + ' 个版本' + (data.running ? ' · 还有一次运行未完成，完成后重新打开可查看' : '');
      if (!versions.length) { info.textContent = '暂无已完成版本' + (data.running ? '，正在运行中。' : '。'); return; }
      versions.forEach(function (version, index) {
        const option = node('option', (index === 0 ? '最新 · ' : '')
          + version.run_id + ' · ' + (version.ok ? '成功' : '失败'));
        option.value = String(index);
        select.appendChild(option);
      });
      select.hidden = false;
      preview.hidden = false;
      report.hidden = false;
      /**
       * 选择一个版本并预览；失败版本保留错误说明和报告入口。
       * @return {Promise<void>} 当前预览请求结束
       */
      async function showVersion() {
        const current = ++versionRequest;
        const version = versions[Number(select.value)];
        report.href = '/report/' + encodeURIComponent(version.run_id);
        detail.textContent = version.ok
          ? '成功 · 总耗时 ' + (version.total == null ? '—' : version.total + 's')
            + (version.finished_at ? ' · 完成于 ' + new Date(version.finished_at * 1000).toLocaleString() : '')
          : '失败：' + (version.error || '未生成图片');
        root.textContent = version.ok && version.svg_bytes ? '正在加载预览…' : '这次没有成功出图';
        if (!version.ok || !version.svg_bytes) { return; }
        const params = query(site, model);
        params.set('run_id', version.run_id);
        try {
          const image = await request('/api/gallery/svg?' + params);
          if (!dialog.open || current !== versionRequest) { return; }
          // 此接口只返回跑批时已清洗的 SVG。保留活 DOM 才能播放 SMIL 动画；
          // shadow root 隔离历史图的样式与重复 id，不把模型文本当 HTML 拼入。
          root.innerHTML = '<style>:host{display:flex;align-items:center;justify-content:center;}'
            + 'svg{max-width:100%;max-height:100%;height:auto;}</style>' + image.svg;
        } catch (error) {
          if (dialog.open && current === versionRequest) { root.textContent = '预览读取失败：' + error.message; }
        }
      }
      select.addEventListener('change', showVersion);
      await showVersion();
    } catch (error) {
      if (dialog.open) { info.textContent = '历史读取失败：' + error.message; }
    }
  }

  /**
   * 在一个模型卡片挂载操作按钮。每张卡片独立轮询，不占用跑批页的计时器。
   * @param {HTMLElement} container 卡片容器
   * @param {string} site 站点名，运行时使用服务端最新保存配置
   * @param {string} model 精确模型名
   * @param {Function} onComplete 可选完成回调，用于首页更新最新图片
   * @return {Object} setRunning 用于同步其他入口启动的运行状态
   */
  function attach(container, site, model, onComplete) {
    const actions = node('div', '', 'model-actions');
    const rerun = node('button', '重新跑');
    rerun.type = 'button';
    rerun.title = '使用当前已保存的站点设置，重新运行这个模型';
    const versions = node('button', '历史版本');
    versions.type = 'button';
    actions.append(rerun, versions);
    const status = node('div', '', 'model-action-status');
    status.setAttribute('role', 'status');
    container.append(actions, status);
    let ownRun = false;
    let externalRunning = false;

    /** 根据本卡提交状态和其他跑批的状态同步按钮，避免轮询提前解锁。 */
    function updateButton() {
      rerun.disabled = ownRun || externalRunning;
      rerun.textContent = rerun.disabled ? '重跑中…' : '重新跑';
    }
    versions.addEventListener('click', function () { history(site, model); });
    rerun.addEventListener('click', async function () {
      if (ownRun || externalRunning) { return; }
      ownRun = true;
      updateButton();
      status.textContent = '正在提交…';
      let run;
      try {
        run = await request('/api/run', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tasks: [{ site: site, model: model }] })
        });
      } catch (error) {
        ownRun = false;
        updateButton();
        status.textContent = '提交失败：' + error.message;
        return;
      }
      status.textContent = '正在运行，完成后可查看新版本。';
      /**
       * 仅轮询已提交批次；网络错误只重试查询，绝不重发收费的生成请求。
       * @return {Promise<void>} 一次状态查询结束
       */
      async function pollRun() {
        try {
          const result = await request('/api/run/' + encodeURIComponent(run.run_id));
          if (result.running) { setTimeout(pollRun, 1500); return; }
          ownRun = false;
          externalRunning = false;
          updateButton();
          const output = (result.results || []).find(function (item) { return item.site === site && item.model === model; });
          status.textContent = output && output.ok ? '已完成 · ' : '本次失败：' + ((output && output.error) || '没有结果') + ' · ';
          const link = node('a', '查看报告');
          link.href = '/report/' + encodeURIComponent(run.run_id);
          link.target = '_blank';
          link.rel = 'noopener';
          status.appendChild(link);
          if (onComplete) { onComplete(); }
        } catch (error) {
          status.textContent = '进度读取失败，正在重试：' + error.message;
          setTimeout(pollRun, 3000);
        }
      }
      pollRun();
    });
    return {
      /**
       * 接收卡片墙最新运行状态，不覆盖本卡尚未拿到结果的提交。
       * @param {boolean} running 该组合是否还有待完成请求
       */
      setRunning: function (running) { externalRunning = Boolean(running); updateButton(); }
    };
  }
  return { attach: attach, history: history };
}());
