/** Shared, local-only UI helpers; never own evaluation state or submit requests. */
(function () {
  'use strict';

  /**
   * Build a Lucide icon from the bundled library; no network or HTML parsing.
   * @param {string} name Known kebab-case Lucide name.
   * @return {SVGElement|null} Decorative icon, or null when the library is unavailable.
   */
  function icon(name) {
    const key = name.replace(/(^|-)(\w)/g, (_, dash, letter) => letter.toUpperCase());
    const source = window.lucide?.icons[key];
    if (!source) { return null; }
    const result = window.lucide.createElement(source);
    result.setAttribute('aria-hidden', 'true');
    result.setAttribute('focusable', 'false');
    result.setAttribute('class', 'lucide');
    return result;
  }

  /**
   * Show a panel without recreating its controls or interrupting a running request.
   * @param {string} group Fixed benchmark group: iq or swe.
   * @param {string} name Fixed panel: rank, setup, or records.
   * @param {boolean} focus Move focus to the active tab for programmatic drilldowns.
   */
  function showPanel(group, name, focus = false) {
    const tabs = document.querySelector('[data-panel-tabs="' + group + '"]');
    const selected = tabs?.querySelector('[data-panel="' + name + '"]');
    if (!selected) { return; }
    // Hide, don't remove: drafts, selection, progress and event handlers must survive.
    document.querySelectorAll('[data-panel-group="' + group + '"]').forEach(panel => {
      panel.hidden = panel.dataset.panel !== name;
    });
    tabs.querySelectorAll('[role=tab]').forEach(tab => {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    if (focus) { selected.focus({ preventScroll: true }); }
  }

  /**
   * Build an actionable empty result, keeping benchmark data separate from UI copy.
   * @param {string} title Empty state title.
   * @param {string} description Why there are no comparable results.
   * @param {string} group Benchmark whose setup should open.
   * @return {HTMLElement} Safe DOM subtree.
   */
  function emptyState(title, description, group) {
    const box = document.createElement('div'); box.className = 'bench-empty';
    const symbol = icon('chart-no-axes-column');
    if (symbol) { box.append(symbol); }
    const heading = document.createElement('h4'); heading.textContent = title;
    const copy = document.createElement('p'); copy.textContent = description;
    const action = document.createElement('button'); action.type = 'button'; action.textContent = '新建评测';
    action.addEventListener('click', () => showPanel(group, 'setup', true));
    box.append(heading, copy, action); return box;
  }

  /** Keep the current destination visible when the sidebar becomes a mobile nav. */
  function revealNavigation() {
    const navigation = document.querySelector('.top nav');
    const active = navigation?.querySelector('[aria-current=page]');
    if (active && navigation.scrollWidth > navigation.clientWidth) {
      navigation.scrollLeft = active.offsetLeft - navigation.offsetLeft - navigation.clientWidth / 2 + active.offsetWidth / 2;
    }
  }

  /** Initialize roving keyboard focus and icons once; navigation never sends API calls. */
  function init() {
    document.querySelector('.skip-link')?.addEventListener('click', event => {
      // The app uses hashes for routing; a plain #main link would switch its view.
      event.preventDefault(); document.getElementById('main').focus();
    });
    document.querySelectorAll('[data-icon]').forEach(button => {
      const symbol = icon(button.dataset.icon);
      if (!symbol) { return; }
      const title = button.textContent.trim();
      if (button.dataset.iconOnly === 'true') {
        button.setAttribute('aria-label', title); button.title = title;
        button.replaceChildren(symbol); button.classList.add('icon-button');
      } else { button.prepend(symbol); }
    });
    window.lucide?.createIcons({ attrs: { 'aria-hidden': 'true', focusable: 'false' } });
    document.querySelectorAll('[data-panel-tabs]').forEach(tabs => {
      const buttons = Array.from(tabs.querySelectorAll('[role=tab]'));
      buttons.forEach(button => button.addEventListener('click', () => showPanel(tabs.dataset.panelTabs, button.dataset.panel)));
      tabs.addEventListener('keydown', event => {
        const index = buttons.indexOf(document.activeElement);
        if (index < 0) { return; }
        const targets = { ArrowRight: (index + 1) % buttons.length, ArrowLeft: (index + buttons.length - 1) % buttons.length, Home: 0, End: buttons.length - 1 };
        if (!(event.key in targets)) { return; }
        event.preventDefault();
        const next = buttons[targets[event.key]];
        showPanel(tabs.dataset.panelTabs, next.dataset.panel, true);
      });
    });
    document.querySelectorAll('[data-open-panel]').forEach(button => {
      button.addEventListener('click', () => {
        const [group, name] = button.dataset.openPanel.split(':'); showPanel(group, name, true);
      });
    });
    window.addEventListener('resize', revealNavigation);
    revealNavigation();
  }

  window.BenchUI = { icon, showPanel, emptyState };
  init();
}());
