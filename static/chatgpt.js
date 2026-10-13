/** 本站官方账号控件。页面只持有授权链接与状态，不接收登录凭据。 */
(function () {
  'use strict';
  /** 每张账号卡片拥有独立请求和计时器，两个品牌登录不能互相覆盖状态。 */
  function accountComponent(provider, brand, domains) {
  let timer = null;
  let busy = false;
  let status = null;
  let host = null;
  let localReady = false;
  let lastState = null;
  let siteName = null;
  let region = 'mainland-cn';

  /** 创建纯文本节点；官方返回的账号信息也不作为 HTML 解析。 */
  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text) { result.textContent = text; }
    if (className) { result.className = className; }
    return result;
  }

  /** 只接受 HTTPS 官方登录地址，避免把异常响应做成可点击的任意跳转。 */
  function safeUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && domains.includes(url.hostname)
        && !url.username && !url.password && (!url.port || url.port === '443') ? url.href : null;
    } catch (_) { return null; }
  }

  /** 调用固定账号接口；禁止缓存含一次性登录链接的响应。 */
  async function request(action, data) {
    const response = await fetch('/api/' + provider + '/' + action, data === undefined
      ? { cache: 'no-store' }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data), cache: 'no-store' });
    const result = await response.json();
    if (!response.ok || result.error) { throw new Error(result.error || '账号请求失败'); }
    return result;
  }

  /** 生成命令按钮，交给现有 Lucide 渲染器处理图标。 */
  function button(label, icon, action) {
    const result = node('button', label);
    result.type = 'button';
    result.dataset.icon = icon;
    result.disabled = busy;
    result.addEventListener('click', action);
    return result;
  }

  /** 只渲染账号管理；评测由独立页面持有，刷新登录状态不能重挂载或重置评测。
   * @param {string} [error] 可展示的账号错误，作为纯文本输出。
   */
  function render(error) {
    if (!host?.isConnected) { return; }
    host.replaceChildren();
    host.setAttribute('aria-busy', String(busy));
    const signedIn = status?.state === 'signed_in';
    const pending = status?.state === 'pending' || status?.state === 'starting';
    const title = node('strong', signedIn ? brand + ' 已登录' : pending ? '等待 ' + brand + ' 授权' : brand + ' 账号');
    const label = node('p', error || (busy ? '正在处理…' : status?.message || (signedIn
      ? '账号可用' : localReady ? '当前使用本机账号，可在此独立登录。' : '尚未登录')));
    label.setAttribute('role', error ? 'alert' : 'status');
    label.setAttribute('aria-live', 'polite');
    host.append(title, label);
    if (signedIn) {
      host.append(node('p', [status.account?.email, status.account?.plan].filter(Boolean).join(' · '), 'chatgpt-identity'));
    }
    const actions = node('div', null, 'chatgpt-actions');
    if (pending) {
      const url = safeUrl(status.login?.url);
      if (status.login?.user_code) {
        const code = node('div', null, 'chatgpt-device-code');
        code.append(node('span', '设备码'), node('strong', status.login.user_code));
        host.append(code);
      }
      if (url) {
        const link = node('a', status.login.kind === 'device' ? '打开验证页面' : '前往 ' + brand + ' 授权', 'chatgpt-authorize');
        link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
        actions.append(link);
      }
      actions.append(button('取消登录', 'x', () => mutate('cancel', {})));
      if (provider === 'chatgpt' && status.login?.kind !== 'device') {
        actions.append(button('改用设备码', 'key-round', () => mutate('login', { kind: 'device' })));
      }
    } else if (signedIn) {
      actions.append(button('退出本站账号', 'log-out', () => mutate('logout', {})));
    } else {
      if (provider === 'kimi') {
        const select = node('select');
        select.setAttribute('aria-label', 'Kimi 账号区域');
        for (const [value, label] of [['mainland-cn', '中国区 kimi.com'], ['global', '国际区 kimi.ai']]) {
          const option = node('option', label); option.value = value; select.append(option);
        }
        select.value = region; select.disabled = busy;
        select.addEventListener('change', () => { region = select.value; });
        actions.append(select);
      }
      const login = button('登录 ' + brand, 'log-in', () => mutate('login', provider === 'kimi' ? { region } : { kind: 'browser' }));
      login.className = 'primary'; actions.append(login);
      if (provider === 'chatgpt') { actions.append(button('使用设备码', 'key-round', () => mutate('login', { kind: 'device' }))); }
    }
    actions.append(button('刷新状态', 'refresh-cw', refresh));
    host.append(actions);
  }

  /** 接收最新状态；认证变化通知各评测页刷新目录，不重新启动或重评历史任务。 */
  function accept(result) {
    status = result;
    const stateKey = [status.state, status.account?.email, status.account?.plan].join('|');
    if (lastState !== null && lastState !== stateKey) {
      document.dispatchEvent(new CustomEvent(provider + '-account-changed', { detail: { name: siteName } }));
    }
    lastState = stateKey;
    if (status.state === 'signed_in') { localReady = false; }
  }

  /** 仅授权期间轮询，使用串行计时器，不叠加尚未返回的请求。 */
  function plan() {
    clearTimeout(timer);
    if (['starting', 'pending'].includes(status?.state) && host?.isConnected) { timer = setTimeout(() => refresh(true), 2000); }
  }

  /** 读取状态；失败保留待授权信息，并显示可操作的错误，不伪称登录完成。 */
  async function refresh(quiet = false) {
    if (busy) { return; }
    busy = true;
    const before = JSON.stringify(status);
    if (quiet !== true) { render(); }
    let error = '';
    try { accept(await request('status')); } catch (e) { error = e.message; }
    finally {
      busy = false;
      // 未变的后台轮询保留授权链接及键盘焦点，不每两秒拆掉当前控件。
      if (quiet !== true || error || before !== JSON.stringify(status)) { render(error); }
      plan();
    }
  }

  /** 发起显式登录、取消或退出；立即显示反馈，防止连点产生多个授权流程。 */
  async function mutate(action, data) {
    if (busy) { return; }
    clearTimeout(timer); busy = true; render();
    let error = '';
    try { accept(await request(action, data)); } catch (e) { error = e.message; }
    finally { busy = false; render(error); plan(); }
  }

  return {
    /** 挂载到只读官方账号卡片；此操作不挂载或启动模型评测。
     * @param {HTMLElement} container 账号设置的父节点。
     * @param {object} site 脱敏站点元信息，仅用于账号来源和就绪状态。
     */
    mount(container, site) {
      clearTimeout(timer);
      host = node('div', null, 'chatgpt-account');
      container.append(host);
      siteName = site.name;
      localReady = provider === 'chatgpt' && site.ready && site.auth_source !== 'site';
      render(); refresh();
    }
  };
  }
  window.ChatGPTAccount = accountComponent('chatgpt', 'ChatGPT', ['auth.openai.com', 'chatgpt.com', 'auth0.openai.com']);
  window.KimiAccount = accountComponent('kimi', 'Kimi', ['auth.kimi.com', 'auth.kimi.ai', 'kimi.com', 'www.kimi.com', 'kimi.ai', 'www.kimi.ai']);
}());
