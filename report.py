"""把跑批结果渲染成一个自包含的 HTML 报告。

自包含的意思是：不引任何外链 CDN、字体或图片，整个文件拷走、断网也能打开。
这也和 svg_sanitize 里去掉外部引用的取向一致。

XSS 上的分工：SVG 是唯一以真实标签插入的内容，已由 svg_sanitize 清洗；
其余所有来自模型或站点的文本（模型名、错误信息、原始输出）一律走 _esc 转义，
绝不直接拼进 HTML。
"""

from __future__ import annotations

import html
import json
import os
import time
from typing import Any

import config

_CSS = """
:root {
  color-scheme: light dark;
  --surface: #fff; --inset: #f5f5f7; --border: #e5e5e9;
  --text: #1d1d1f; --secondary: #6e6e73; --blue: #0071e3;
}
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 2rem 1.5rem 4rem; max-width: 1280px;
  font: .875rem/1.55 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: #f6f6f8; color: var(--text); font-optical-sizing: auto;
}
h1 { font-size: clamp(1.5rem, 3vw, 2rem); line-height: 1.25; letter-spacing: -.03em; margin: 0 0 .5rem; }
h2 { font-size: 1.05rem; font-weight: 600; margin: 2rem 0 .8rem; }
a { color: var(--blue); }
.back-link { display: inline-flex; align-items: center; min-height: 2.75rem; margin-bottom: .75rem; text-decoration: none; }
.back-link:hover { text-decoration: underline; }
.meta { color: var(--secondary); font-size: .8rem; margin: .65rem 0; overflow-wrap: anywhere; }
.summary { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); margin: 1.5rem 0; border: 1px solid var(--border); border-radius: 1rem; background: var(--surface); overflow: hidden; }
.stat { padding: 1rem 1.25rem; }
.stat + .stat { border-left: 1px solid var(--border); }
.stat .k { color: var(--secondary); font-size: .75rem; }
.stat .v { font-size: 1.35rem; font-weight: 600; margin-top: .3rem; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.table-scroll { overflow-x: auto; border: 1px solid var(--border); border-radius: .8rem; }
table { width: 100%; border-collapse: collapse; background: var(--surface); font-size: .8rem; }
th, td { padding: .7rem .8rem; border-bottom: 1px solid var(--border); text-align: right; white-space: nowrap; }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
th { background: var(--inset); color: var(--secondary); font-weight: 500; }
th button { background: transparent; color: inherit; border: 0; padding: .3rem 0; font: inherit; cursor: pointer; min-height: 2rem; touch-action: manipulation; }
th button:hover { color: var(--text); }
th.sorted button::after { content: " ↓"; }
th.sorted.asc button::after { content: " ↑"; }
button:active { opacity: .65; }
:where(button, a, summary, .table-scroll):focus-visible { outline: 3px solid var(--blue); outline-offset: 3px; }
tr:last-child td { border-bottom: 0; }
tr.bad td { color: #b3261e; }
td:last-child { white-space: normal; min-width: 5rem; max-width: 20rem; overflow-wrap: anywhere; }
.tag { margin-left: .6rem; color: var(--secondary); font-size: .7rem; font-weight: 400; }
.wall { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1rem; }
.card { display: flex; flex-direction: column; min-width: 0; background: var(--surface); border: 1px solid var(--border); border-radius: .85rem; overflow: hidden; }
.card header { padding: .85rem 1rem; }
.card .model { font-weight: 600; overflow-wrap: anywhere; }
.card .site { color: var(--secondary); font-size: .75rem; }
/* 模型通常针对浅底作图，暗色界面也保留独立白色画布。 */
.card .fig { height: 240px; display: flex; align-items: center; justify-content: center; overflow: hidden; padding: .6rem; margin: 0 .65rem; border-radius: .5rem; background: #fff; }
.card .fig svg { max-width: 100%; max-height: 100%; height: auto; }
.card .nums { padding: .75rem 1rem; color: var(--secondary); font-size: .75rem; }
.card .fail { color: #b3261e; padding: 1.25rem 1rem; font-size: .8rem; overflow-wrap: anywhere; }
details { padding: 0 1rem .75rem; }
summary { cursor: pointer; color: var(--secondary); font-size: .75rem; min-height: 2rem; }
details pre { max-height: 260px; overflow: auto; background: var(--inset); padding: .75rem; border-radius: .5rem; font-size: .75rem; white-space: pre-wrap; overflow-wrap: anywhere; }
.note { color: var(--secondary); font-size: .75rem; padding: 0 1rem .5rem; }
@media (max-width: 640px) {
  body { padding: 1rem .9rem 3rem; }
  .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stat + .stat { border-left: 0; }
  .stat:nth-child(even) { border-left: 1px solid var(--border); }
  .stat:nth-child(n+3) { border-top: 1px solid var(--border); }
  .stat:last-child { grid-column: 1 / -1; }
  .wall { grid-template-columns: minmax(0, 1fr); }
  th button, summary { min-height: 2.75rem; }
}
@media (prefers-color-scheme: dark) {
  :root { --surface: #242426; --inset: #303033; --border: #39393e; --text: #f5f5f7; --secondary: #aaaab1; --blue: #4c9fff; }
  body { background: #171719; }
  tr.bad td, .card .fail { color: #ff9b94; }
}
@media (prefers-contrast: more) {
  :root { --border: #757578; --secondary: #48484b; }
}
@media (prefers-contrast: more) and (prefers-color-scheme: dark) {
  :root { --border: #99999e; --secondary: #dedee2; }
}

"""

# 表格排序脚本。写死在报告里而不是引外部 js，保证单文件可携带。
# 注意这里只操作已有 DOM 节点、不拼 innerHTML，避免给报告再引入一个注入点。
_JS = """
document.querySelectorAll('table').forEach(function (table) {
  var headers = table.querySelectorAll('th');
  headers.forEach(function (th, idx) {
    th.querySelector('button').addEventListener('click', function () {
      var tbody = table.tBodies[0];
      var rows = Array.prototype.slice.call(tbody.rows);
      // 首次升序；再次点击同一列反转，列切换恢复升序。
      var asc = !(th.classList.contains('sorted') && th.classList.contains('asc'));
      headers.forEach(function (h) {
        h.classList.remove('sorted', 'asc');
        h.setAttribute('aria-sort', 'none');
      });
      th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
      th.classList.add('sorted');
      if (asc) { th.classList.add('asc'); }
      rows.sort(function (a, b) {
        var x = a.cells[idx].dataset.v, y = b.cells[idx].dataset.v;
        var nx = parseFloat(x), ny = parseFloat(y);
        // 无值的行（失败用例）恒沉底，不让它们混在最快/最慢里干扰阅读。
        var xEmpty = x === '' || x === undefined, yEmpty = y === '' || y === undefined;
        if (xEmpty && yEmpty) { return 0; }
        if (xEmpty) { return 1; }
        if (yEmpty) { return -1; }
        if (!isNaN(nx) && !isNaN(ny)) { return asc ? nx - ny : ny - nx; }
        return asc ? String(x).localeCompare(y) : String(y).localeCompare(x);
      });
      rows.forEach(function (r) { tbody.appendChild(r); });
    });
  });
});
"""


def _esc(value: Any) -> str:
    """HTML 转义。

    所有来自模型或站点的文本都必须过这里。quote=True 是必需的，
    因为部分值会落进属性值（如 data-v）。

    @param value 任意值，None 按空串处理
    @return 转义后的字符串
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _num(value: Any, suffix: str = "", digits: int = 2) -> str:
    """格式化数字单元格，无值时显示 "-"。

    显示 "-" 而不是 0，是为了区分"没有这项数据"和"数值确实是 0"，
    尤其费用字段：显示 0 会被误读成免费。

    @param value 数值或 None
    @param suffix 单位后缀
    @param digits 小数位
    @return 展示用字符串
    """
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return _esc(value)


def _cell(value: Any, text: str | None = None) -> str:
    """生成一个带排序值的单元格。

    data-v 存原始值供 JS 排序用，显示值单独格式化，这样排序不会被 "1.23s" 这种
    带单位的字符串破坏。

    @param value 排序用原始值
    @param text 显示文本，缺省时用 value
    @return td 的 HTML 片段
    """
    sort_v = "" if value is None else _esc(value)
    return f'<td data-v="{sort_v}">{_esc(text) if text is not None else _esc(value)}</td>'


def render(snapshot: dict[str, Any], *, interactive: bool = False) -> str:
    """渲染整份报告。

    @param snapshot bench.Run.snapshot(include_svg=True) 的输出
    @param interactive 在线报告启用重跑和历史入口；导出的单文件保持离线可读
    @return 完整 HTML 文本
    """
    results = snapshot.get("results") or []
    # 表格默认按总时延升序：跑批的第一诉求是"谁快"，让最快的直接排在最上面。
    ordered = sorted(results, key=lambda r: (not r.get("ok"), r.get("total") if r.get("total") is not None else 1e9))

    finished = snapshot.get("finished_at")
    # 运行快照没有完成时间，不能用当前时间冒充完成时间。
    if snapshot.get("running"):
        status = f'运行中 · 已完成 {snapshot.get("done", len(results))}/{snapshot.get("total", len(results))} 项'
    elif finished:
        status = "完成于 " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(finished))
    else:
        status = "完成时间未记录"
    ok_count = sum(1 for r in results if r.get("ok"))
    costs = [r["cost"]["total"] for r in results if isinstance(r.get("cost"), dict)]
    ttfts = [r["ttft"] for r in results if r.get("ok") and r.get("ttft") is not None]

    parts: list[str] = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>鹈鹕骑车跑批 {_esc(snapshot.get('run_id'))}</title>",
        f"<style>{_CSS}</style></head><body>",
        '<a class="back-link" href="/">‹ 返回画廊</a>' if interactive else "",
        "<h1>评测报告</h1>",
        f'<div class="meta">{_esc(status)}<br>批次 {_esc(snapshot.get("run_id"))}</div>',
        '<div class="summary">',
        _stat("用例数", str(snapshot.get("total") or len(results))),
        _stat("成功出图", str(ok_count)),
        _stat("最快 TTFT", _num(min(ttfts), "s") if ttfts else "-"),
        _stat("TTFT 中位", _num(_median(ttfts), "s") if ttfts else "-"),
        _stat("总费用", f"${sum(costs):.4f}" if costs else "暂无数据"),
        "</div>",
        "<h2>指标表<span class=\"tag\">点表头可排序</span></h2>",
        _table(ordered),
        "<h2>模型结果</h2>",
        '<div class="wall">',
    ]
    parts.extend(_card(r) for r in ordered)
    parts.append("</div>")
    if interactive:
        # 只有本地服务提供的报告加载操作组件。导出文件不依赖服务，也不能
        # 在被转发到其他网站后自动对那个网站发起模型运行请求。
        parts.append('<link rel="stylesheet" href="/static/model-actions.css">')
        parts.append('<script src="/static/model-actions.js"></script>')
        parts.append('''<script>
document.querySelectorAll('.card').forEach(function (card) {
  ModelActions.attach(card, card.dataset.site, card.dataset.model);
});
</script>''')
    parts.append(f"<script>{_JS}</script></body></html>")
    return "\n".join(parts)


def _stat(label: str, value: str) -> str:
    """渲染一个概览数字块。

    @param label 指标名
    @param value 展示值
    @return HTML 片段
    """
    return f'<div class="stat"><div class="k">{_esc(label)}</div><div class="v">{_esc(value)}</div></div>'


def _median(values: list[float]) -> float:
    """取中位数。用中位数而不是平均值，避免单个超时用例把整体拉偏。

    @param values 非空数值列表
    @return 中位数
    """
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _table(results: list[dict[str, Any]]) -> str:
    """渲染指标表。

    @param results 已排序的结果列表
    @return table 的 HTML
    """
    head = [
        "站点", "模型", "首token(s)", "响应头(s)", "总耗时(s)",
        "输出tok/s", "输入tok", "输出tok", "费用($)", "SVG(KB)", "状态",
    ]
    rows: list[str] = []
    for r in results:
        cost = r.get("cost") if isinstance(r.get("cost"), dict) else None
        est = " *" if r.get("tokens_estimated") else ""
        status = "成功" if r.get("ok") else (r.get("error") or "失败")
        rows.append(
            f'<tr class="{"" if r.get("ok") else "bad"}">'
            + _cell(r.get("site"))
            + _cell(r.get("model"))
            + _cell(r.get("ttft"), _num(r.get("ttft")))
            + _cell(r.get("ttfb"), _num(r.get("ttfb")))
            + _cell(r.get("total"), _num(r.get("total")))
            + _cell(r.get("output_tps"), _num(r.get("output_tps"), digits=1))
            + _cell(r.get("prompt_tokens"), f'{r.get("prompt_tokens") or "-"}{est}')
            + _cell(r.get("completion_tokens"), f'{r.get("completion_tokens") or "-"}{est}')
            + _cell(cost["total"] if cost else None, f'{cost["total"]:.4f}' if cost else "-")
            + _cell(r.get("svg_bytes"), _num((r.get("svg_bytes") or 0) / 1024, digits=1))
            + _cell(status)
            + "</tr>"
        )
    return (
        '<div class="table-scroll" role="region" aria-label="评测指标，可横向滚动" tabindex="0"><table><thead><tr>'
        + "".join(f'<th scope="col" aria-sort="none"><button type="button">{_esc(h)}</button></th>' for h in head)
        + "</tr></thead><tbody>"
        + "".join(rows)
        + '</tbody></table></div><div class="meta">带 * 的 token 数是按字符估算的（站点没返回 usage），费用同为估算值。</div>'
    )


def _card(r: dict[str, Any]) -> str:
    """渲染一张 SVG 卡片。

    svg 字段是唯一不转义直接插入的内容，它已经过 svg_sanitize.sanitize()。
    原始输出放在 details 里且必须转义，否则模型返回的任意 HTML 会在这里执行。
    data 属性也必须转义；在线操作从这些结构化属性读取名称，不拆分展示文本。

    @param r 单个结果
    @return card 的 HTML
    """
    head = (
        f'<header><div class="model">{_esc(r.get("model"))}</div>'
        f'<div class="site">{_esc(r.get("site"))}</div></header>'
    )

    if r.get("ok") and r.get("svg"):
        # 已清洗，这里刻意不转义，否则卡片墙只会显示一堆源码文本。
        body = f'<div class="fig">{r["svg"]}</div>'
    else:
        body = f'<div class="fail">{_esc(r.get("error") or "无输出")}</div>'

    cost = r.get("cost") if isinstance(r.get("cost"), dict) else None
    nums = " · ".join(filter(None, [
        f'TTFT {_num(r.get("ttft"), "s")}' if r.get("ttft") is not None else None,
        f'总 {_num(r.get("total"), "s")}' if r.get("total") is not None else None,
        f'{r.get("completion_tokens")} tok' if r.get("completion_tokens") else None,
        f'${cost["total"]:.4f}' if cost else None,
    ])) or "无指标"

    notes = r.get("sanitize_notes") or []
    note_html = f'<div class="note">已清洗：{_esc("；".join(notes))}</div>' if notes else ""

    raw = r.get("raw") or ""
    raw_html = ""
    if raw:
        # 原始输出可能很长，截断到 20000 字符，够判断"它到底输出了什么"。
        clipped = raw[:20000] + ("\n…（已截断）" if len(raw) > 20000 else "")
        raw_html = f"<details><summary>原始输出</summary><pre>{_esc(clipped)}</pre></details>"

    return (
        f'<div class="card" data-site="{_esc(r.get("site"))}" data-model="{_esc(r.get("model"))}">'
        f'{head}{body}<div class="nums">{_esc(nums)}</div>'
        f"{note_html}{raw_html}</div>"
    )


def write(snapshot: dict[str, Any]) -> str:
    """把报告写到跑批目录下。

    @param snapshot 含 svg 的完整快照
    @return 生成的 HTML 文件绝对路径
    @throws OSError 目录不可写
    """
    run_dir = os.path.join(config.RUNS_DIR, str(snapshot.get("run_id") or "unknown"))
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "report.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render(snapshot))
    return path


if __name__ == "__main__":
    # 便于单独调试：python3 report.py runs/<id>/results.json
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("用法：python3 report.py <results.json>")
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(write(json.load(fh)))
