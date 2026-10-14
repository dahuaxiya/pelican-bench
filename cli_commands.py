"""命令行控制台；复用本机服务的配置、任务和判分，不另建测评执行器。"""
from __future__ import annotations

import argparse
from collections import deque
import json
import math
import os
import sys
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid

from runtime_paths import DATA_DIR

KINDS = ("performance", "capacity", "iq", "svg", "engineering")


class ApiError(ValueError):
    """保留 HTTP 状态，只有明确的忙碌响应允许等待重试。"""

    def __init__(self, message, status):
        """记录服务端诊断和状态码，不包含请求头或凭据。"""
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """实例控制请求不得跟随重定向，把本机控制令牌发送到其他服务。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """拒绝所有跳转；服务迁移必须重新核对 runtime.json。"""
        return None


class Client:
    """只连接经随机实例标识校验的回环服务。"""

    def __init__(self, metadata):
        """由本机运行记录建立连接，不接受任意服务 URL。"""
        port = metadata.get("port")
        if type(port) is not int or not 1 <= port <= 65535 or not metadata.get("instance"):
            raise ValueError("无效的运行记录")
        self.metadata = metadata
        self.url = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, body=None, timeout=60):
        """请求当前实例；无自动重发，避免响应丢失后重复产生模型费用。

        Args:
            path: 程序内固定 API 路径，用户 ID 必须先经过 quote。
            body: None 为 GET，否则发送 JSON POST。
            timeout: 本机控制请求超时，不是模型请求预算。
        """
        headers = {"Content-Type": "application/json", "Origin": self.url,
                   "X-Pelican-Instance": self.metadata["instance"]}
        if path == "/api/runtime/stop":
            headers["X-Pelican-Control"] = self.metadata.get("control_token", "")
        req = urllib.request.Request(self.url + path, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                message = json.load(exc).get("error", f"HTTP {exc.code}")
            except (ValueError, AttributeError):
                message = f"HTTP {exc.code}"
            raise ApiError(str(message), exc.code) from None


def discover():
    """只读检查本机实例；旧 PID、损坏记录或被其他进程占用的端口都不算存活。"""
    try:
        metadata = json.loads((DATA_DIR / "runtime.json").read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("state") != "running":
            return None
        client = Client(metadata)
        live = client.request("/api/runtime", timeout=.5)
        if live == {"application": "pelican-bench", "instance": metadata["instance"]}:
            return client
    except (OSError, ValueError):
        pass
    return None


def _metric(value, suffix=""):
    """将实测数字格式化为两位小数，未知或非法指标不能显示成零。

    Args:
        value: 原始指标，只有有限且非负的数字有效；布尔值不算数字。
        suffix: 文本输出的固定单位，不修改 JSON 中的数值。
    Returns:
        带单位的数字；未知值返回 -。
    """
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return "-"
    return f"{value:.2f}{suffix}"


def _first_text_seconds(row):
    """优先读取最终首字指标，在途请求只接受真实正文增量的计时。

    Args:
        row: 网页与 CLI 共用的单模型快照，不修改原数据。
    Returns:
        原始精度的秒数；完整消息事件、旧记录或失败后的部分输出返回 None。
    """
    value = (row.get("metrics") or {}).get("ttft_seconds")
    if _metric(value) != "-":
        return value
    observation = row.get("output_observation") or {}
    # 旧 exec 渠道的 message_completed 只证明整条回答到达，不能拿来补造首字。
    if row.get("status") == "running" and observation.get("kind") == "text_delta":
        value = observation.get("elapsed_seconds")
        if _metric(value) != "-":
            return value
    return None


def emit(value, as_json=False):
    """输出结果；文本统一指标格式，JSON 保留原始精度和未知值。

    Args:
        value: 来自本机服务的结果快照。
        as_json: 为 True 时只输出 JSON，不加入表头或进度文字。
    """
    if as_json or not isinstance(value, dict):
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    common = ("state", "id", "run_id", "status", "url", "data_directory", "log", "message")
    for key in common:
        if key in value:
            print(f"{key}: {value[key]}")
    if value.get("kind") == "network":
        model_mode = value.get("options", {}).get("mode") == "model"
        print("节点\t成功/尝试\t首字中位耗时\t端到端吞吐\t总耗时中位数" if model_mode else "节点\t成功/尝试\t握手中位延迟\t最慢成功\t波动（极差）")
        for row in value.get("rows", []):
            if model_mode:
                print(f'{row["name"]}\t{row["success"]}/{row["attempts"]}\t'
                      f'{_metric(row.get("median_ttft_ms"), "ms")}\t{_metric(row.get("median_tokens_per_second"), "token/s")}\t'
                      f'{_metric(row.get("median_ms"), "ms")}')
                continue
            print(f'{row["name"]}\t{row["success"]}/{row["attempts"]}\t'
                  f'{_metric(row["median_ms"], "ms")}\t{_metric(row["max_success_ms"], "ms")}\t'
                  f'{_metric(row["spread_ms"], "ms")}')
        print("节点恢复: " + json.dumps(value.get("restoration"), ensure_ascii=False))
        if value.get("failure"):
            print(value["failure"])
        if value.get("recommendations"):
            print("建议主用 / 备用: " + " / ".join(value["recommendations"]))
        if value.get("application"):
            application = value["application"]
            print("节点切换: " + application["status"] + " / " + application["target"])
            print("切换后核对: " + json.dumps(application.get("restoration"), ensure_ascii=False))
            if application.get("failure"):
                print(application["failure"])
    elif "rows" in value:
        print("站点 / 模型\t状态\t端到端 token/s\t首字延迟 (TTFT)\t容量信号")
        for row in value["rows"]:
            metrics = row.get("metrics") or {}
            print(f'{row.get("site", "")} / {row.get("model", "")}\t{row.get("status", "")}\t'
                  f'{_metric(metrics.get("end_to_end_tokens_per_second"))}\t{_metric(_first_text_seconds(row), "s")}\t'
                  f'{"有" if row.get("capacity_detected") else "未发现"}')
            if row.get("reason"):
                print("  " + row["reason"])
    elif "summary" in value:
        print("站点 / 模型\t最终成绩")
        for summary in value["summary"]:
            final = summary.get("final", {})
            score = final.get("ability_score") if final.get("ready") else summary.get("success_rate")
            label = f"{score}%" if score is not None else "未完成"
            print(f'{summary["site"]} / {summary["model"]}\t{label}')
            if final.get("pending"):
                print(f'  待复核: {final["pending"]}')
        if value.get("error") or (value.get("auto_review") or {}).get("error"):
            print(value.get("error") or value["auto_review"]["error"])
        if "questions" in value:
            print(json.dumps({"questions": value["questions"], "results": value.get("results")}, ensure_ascii=False, indent=2))
    else:
        rest = {k: v for k, v in value.items() if k not in common}
        if rest:
            print(json.dumps(rest, ensure_ascii=False, indent=2))


def parser():
    """统一两个发行入口的帮助及参数；旧版省略 start 的调用继续有效。"""
    root = argparse.ArgumentParser(prog="pelican-bench", description="本机模型测评：网页与 CLI 共用配置和历史",
        epilog="全局选项：--data-dir PATH；npm 还支持 --python PATH。用 <command> --help 查看参数。")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("start", "restart"):
        cmd = commands.add_parser(name, help="启动服务" if name == "start" else "停止空闲服务后重新启动")
        cmd.add_argument("--port", type=int, default=8780, help="回环端口，0 为系统分配；默认 8780")
        cmd.add_argument("--background", "-d", action="store_true", help="后台运行，日志写入用户目录")
        cmd.add_argument("--no-browser", action="store_true")
        cmd.add_argument("--json", action="store_true", help="后台启动时输出 JSON")
    cmd = commands.add_parser("stop", help="停止本数据目录的服务；任务运行中默认拒绝")
    cmd.add_argument("--wait", action="store_true", help="等待在途任务完成后停止")
    cmd.add_argument("--timeout", type=int, default=30, help="等待上限秒数，超时保留服务")
    cmd.add_argument("--json", action="store_true")
    for name, help_text in (("status", "查询服务状态"), ("doctor", "只读检查本机依赖")):
        cmd = commands.add_parser(name, help=help_text)
        cmd.add_argument("--json", action="store_true")
    cmd = commands.add_parser("open", help="打开本机网页，未运行时后台启动")
    cmd.add_argument("page", nargs="?", default="performance", choices=("performance", "iq", "home", "settings", "engineering", "network"))
    cmd = commands.add_parser("logs", help="读取后台服务日志")
    cmd.add_argument("--lines", type=int, default=50)
    cmd.add_argument("--follow", "-f", action="store_true")
    cmd = commands.add_parser("setup-iq", help="安装固定版本官方题库")
    cmd.add_argument("dataset", choices=("bbeh", "arc"))
    cmd.add_argument("--archive", type=Path)
    cmd = commands.add_parser("setup-engineering", help="安装可选工程依赖和题库")
    cmd.add_argument("--parquet", type=Path)
    cmd = commands.add_parser("sites", help="列出或修改站点，密钥不在命令参数中传递")
    actions = cmd.add_subparsers(dest="action", required=True)
    actions.add_parser("list").add_argument("--json", action="store_true")
    for name in ("set", "remove"):
        edit = actions.add_parser(name)
        edit.add_argument("name")
        edit.add_argument("--json", action="store_true")
        if name == "set":
            edit.add_argument("--base-url")
            edit.add_argument("--model", action="append", dest="models", help="重复传入可配置多个候选模型")
            secret = edit.add_mutually_exclusive_group()
            secret.add_argument("--api-key-env", help="读取指定环境变量中的密钥")
            secret.add_argument("--api-key-stdin", action="store_true", help="从标准输入读取密钥")
    cmd = commands.add_parser("models", help="查询指定站点的模型目录")
    cmd.add_argument("--site", required=True)
    cmd.add_argument("--json", action="store_true")
    cmd = commands.add_parser("test", help="直接测评，默认等待结束；支持 performance/capacity/iq/svg/engineering")
    kinds = cmd.add_subparsers(dest="kind", required=True)
    for kind in KINDS:
        test = kinds.add_parser(kind)
        test.add_argument("--site", action="append", help="已配置站点名，可重复")
        test.add_argument("--model", action="append", help="模型名，可重复；与站点组成组合")
        test.add_argument("--all-models", action="store_true")
        test.add_argument("--target", action="append", nargs=2, metavar=("SITE", "MODEL"), help="显式组合，可重复；不能与 site/model 混用")
        test.add_argument("--no-wait", action="store_true", help="提交后只返回批次 ID")
        test.add_argument("--json", action="store_true")
        test.add_argument("--wait-timeout", type=int, default=0, help="只限制 CLI 等待，0 为不限；超时不取消任务")
        if kind in ("performance", "capacity", "iq"):
            test.add_argument("--concurrency", type=int, choices=range(1, 201), metavar="1..200")
        if kind in ("iq", "engineering"):
            test.add_argument("--suite", default="bbeh-smoke" if kind == "iq" else "smoke")
            test.add_argument("--repeats", type=int)
            test.add_argument("--max-tokens", type=int)
            test.add_argument("--request-timeout", type=int, help="模型请求超时秒数")
        if kind == "iq":
            test.add_argument("--thinking-mode", choices=("default", "enabled", "disabled"))
        if kind == "engineering":
            test.add_argument("--task-timeout", type=int)
            test.add_argument("--step-limit", type=int)
            test.add_argument("--instance-id", action="append", dest="instance_ids")
    cmd = commands.add_parser("results", help="查看历史列表或一个批次的结果")
    cmd.add_argument("kind", choices=KINDS)
    cmd.add_argument("run_id", nargs="?")
    cmd.add_argument("--watch", action="store_true")
    cmd.add_argument("--full", action="store_true", help="包含完整题目和回答")
    cmd.add_argument("--json", action="store_true")
    cmd.add_argument("--wait-timeout", type=int, default=0)
    for name in ("cancel", "retry"):
        cmd = commands.add_parser(name, help="停止指定批次" if name == "cancel" else "原地重试 IQ 接口失败项")
        cmd.add_argument("kind", choices=("performance", "capacity", "iq", "engineering") if name == "cancel" else ("iq",))
        cmd.add_argument("run_id")
        cmd.add_argument("--json", action="store_true")
        if name == "retry":
            cmd.add_argument("--no-wait", action="store_true")
            cmd.add_argument("--wait-timeout", type=int, default=0)
    vpn = commands.add_parser("vpn", help="VPN 节点握手测速或官方模型首字与吞吐实测")
    actions = vpn.add_subparsers(dest="action", required=True)
    for action in ("config", "discover", "test", "results", "cancel", "recover", "apply"):
        cmd = actions.add_parser(action)
        cmd.add_argument("--json", action="store_true")
        if action == "config":
            cmd.add_argument("--controller")
            cmd.add_argument("--proxy")
            cmd.add_argument("--group")
            cmd.add_argument("--secret-env", help="从环境变量读取控制密钥，不放入命令行")
        elif action == "test":
            cmd.add_argument("--mode", choices=("handshake", "model"), default="handshake")
            cmd.add_argument("--model", help="模型实测使用的官方模型；需要在本站登录 ChatGPT")
            cmd.add_argument("--sort-by", choices=("ttft", "throughput"), default="ttft")
            cmd.add_argument("--repeats", type=int, default=5)
            cmd.add_argument("--top", type=int, default=6, help="HTTPS 初筛候选数；额外包含原节点")
            cmd.add_argument("--concurrency", type=int, default=6, help="节点并发数，默认6，范围1..50；1为串行")
            cmd.add_argument("--timeout", type=int, help="单次总时限；握手默认12秒，模型实测默认180秒")
            cmd.add_argument("--node", action="append", dest="nodes", help="只测指定叶子节点，可重复")
            cmd.add_argument("--no-wait", action="store_true")
            cmd.add_argument("--wait-timeout", type=int, default=0)
        elif action == "results":
            cmd.add_argument("run_id", nargs="?")
            cmd.add_argument("--watch", action="store_true")
            cmd.add_argument("--page", type=int, default=1)
            cmd.add_argument("--wait-timeout", type=int, default=0)
        elif action == "cancel":
            cmd.add_argument("run_id")
        elif action == "apply":
            cmd.add_argument("run_id", nargs="?", help="采用此批次排名；省略则使用当前配置最新完成的批次")
            cmd.add_argument("--no-wait", action="store_true")
            cmd.add_argument("--wait-timeout", type=int, default=0)
    return root


def select_targets(args, client):
    """展开明确选择并去重；不在漏填模型时默认测试整个目录。"""
    if args.target:
        if args.site or args.model or args.all_models:
            raise ValueError("--target 不能与 --site/--model/--all-models 混用")
        pairs = args.target
    else:
        if not args.site or bool(args.model) == args.all_models:
            raise ValueError("需要 --site，以及 --model 或 --all-models 二选一")
        pairs = []
        for site in args.site:
            models = args.model
            if args.all_models:
                catalog = client.request("/api/models?" + urllib.parse.urlencode({"site": site}))
                models = catalog.get("models")
                if catalog.get("error") or not models:
                    raise ValueError(catalog.get("error") or f"{site} 没有可用模型")
            pairs.extend((site, model) for model in models)
    return [{"site": site, "model": model} for site, model in dict.fromkeys(map(tuple, pairs))]


def result_path(kind, run_id, full=False):
    """构造结果路径，用户提供的 ID 只能作为一个转义后的路径段。"""
    prefix = ("/api/network/run/" if kind == "network" else "/api/probe/history/" if kind in ("performance", "capacity") else
              "/api/run/" if kind == "svg" else f'/api/{"swe" if kind == "engineering" else "iq"}/run/')
    return prefix + urllib.parse.quote(run_id, safe="") + ("?full=1" if full else "")


def read_result(client, kind, run_id, full=False):
    """读取指定批次；性能任务在途时补读实时快照，以获取尚未归档的首字事件。

    Args:
        client: 已核对实例身份的本机 API 客户端。
        kind: 已通过参数校验的测评类型。
        run_id: 用户选择的批次 ID，绝不能换成另一个当前批次。
        full: 是否包含题目与回答，只对支持它的测评生效。
    Returns:
        指定批次的实时或历史数据；不同批次的实时数据不会覆盖所选历史。
    """
    data = client.request(result_path(kind, run_id, full))
    if kind in ("performance", "capacity") and pending(kind, data):
        current = client.request("/api/probe")
        # 历史只在状态落盘时更新；首字先出现在内存快照中。两次 GET 之间可能
        # 已开始新批次，此时必须回读原批次，不能把新批次的时间关联到旧模型。
        data = current if current.get("id") == run_id else client.request(result_path(kind, run_id, full))
    return data


def pending(kind, data):
    """等待复核或节点切换验证结束，不能只凭原批次已完成提前返回。"""
    if kind == "network" and data.get("application", {}).get("status") in ("running", "stopping"):
        return True
    if kind == "svg":
        return bool(data.get("running"))
    if data.get("status") in ("running", "stopping", "queued"):
        return True
    if kind == "iq" and data.get("status") == "completed":
        review = data.get("auto_review") or {}
        if review.get("status") in ("queued", "running"):
            return True
        # 保存评测终态和登记复核之间有短暂窗口，由 wait_result 给一次宽限。
        return bool(not review and any(s.get("final", {}).get("pending") for s in data.get("summary", [])))
    return False


def result_exit(kind, data):
    """0 表示完整执行，3 表示接口/环境/复核失败；题目答错本身不是 CLI 故障。"""
    if kind == "network":
        return 0 if data.get("status") == "completed" and data.get("recommendations") else 3
    if kind in ("performance", "capacity"):
        return 0 if data.get("status") == "completed" and all(r.get("status") == "success" for r in data.get("rows", [])) else 3
    if kind == "svg":
        return 0 if not data.get("cancelled") and data.get("done") == data.get("total") and all(r.get("ok") for r in data.get("results", [])) else 3
    if data.get("status") != "completed":
        return 3
    if kind == "iq":
        return 0 if data.get("summary") and all(s.get("final", {}).get("ready") for s in data["summary"]) else 3
    return 0 if data.get("summary") and all(s.get("valid") for s in data["summary"]) else 3


def wait_result(client, kind, run_id, timeout=0, full=False):
    """等待同一批次并报告首字事件；进度写 stderr，stdout 留给最终结果。

    Args:
        client: 已核对身份的本机 API 客户端。
        kind: 测评类型，决定最终完成及自动复核的判定规则。
        run_id: 等待的固定批次 ID。
        timeout: CLI 等待上限秒数，0 为不限；到期不取消后台任务。
        full: 是否请求完整题目与回答。
    Returns:
        最后快照和退出码；用户中断时保留后台任务并传播 KeyboardInterrupt。
    """
    started, last, missing_review = time.monotonic(), None, None
    first_text_reported = set()
    try:
        while True:
            data = read_result(client, kind, run_id, full)
            review = data.get("auto_review") or {}
            rows = data.get("rows", [])
            done = data.get("done", sum(r.get("status") not in ("queued", "running") for r in rows))
            total = data.get("total", len(rows))
            if kind == "network" and data.get("phase") == "https":
                done, total = data.get("screened", 0), data.get("screen_total", 0)
            text = f'{run_id}  {data.get("status", "running" if data.get("running") else "completed")}  {done}/{total}'
            if review:
                text += f'  复核 {review.get("status")} {review.get("done", 0)}/{review.get("total", 0)}'
            if data.get("auto_retry", {}).get("round"):
                text += f'  自动重试第 {data["auto_retry"]["round"]} 轮'
            if data.get("phase"):
                text += "  " + data["phase"]
            if kind == "network" and data.get("options", {}).get("concurrency"):
                text += f'  并发 {data["options"]["concurrency"]}  运行 {data.get("running", 0)}  排队 {data.get("queued", 0)}'
            if kind == "network" and data.get("current_node"):
                text += f'  {data["current_node"]} 第 {data.get("round", 1)} 次'
                if data.get("output_observation"):
                    text += '  首字 ' + _metric(data["output_observation"]["elapsed_seconds"], "s")
            if kind == "network" and data.get("application"):
                application = data["application"]
                text += f'  切换 {application["target"]} {application["status"]} {application["phase"]}'
            if text != last:
                print(text, file=sys.stderr, flush=True)
                last = text
            if kind in ("performance", "capacity"):
                for row in rows:
                    key = (row.get("site", ""), row.get("model", ""))
                    first_text = _first_text_seconds(row)
                    # 每个站点模型只报一次，不随轮询刷屏；JSON 标准输出始终不混入进度。
                    if first_text is not None and key not in first_text_reported:
                        print(f'  {key[0]} / {key[1]}  首字延迟 (TTFT): {_metric(first_text, "s")}',
                              file=sys.stderr, flush=True)
                        first_text_reported.add(key)
            if not pending(kind, data):
                return data, result_exit(kind, data)
            if kind == "iq" and data.get("status") == "completed" and not review:
                missing_review = missing_review or time.monotonic()
                if time.monotonic() - missing_review >= 5:
                    return data, 3
            if timeout and time.monotonic() - started >= timeout:
                print("等待超时，任务仍在后台；使用 results 查询或 cancel 停止。", file=sys.stderr)
                return data, 4
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"已停止等待，批次 {run_id} 保留在后台。", file=sys.stderr)
        raise


def execute_vpn(args, client):
    """Run node operations through the one local service, sharing website history."""
    action, code = args.action, 0
    if action == "config":
        values = {key: getattr(args, key) for key in ("controller", "proxy", "group") if getattr(args, key) is not None}
        if args.secret_env:
            if args.secret_env not in os.environ:
                raise ValueError("控制密钥环境变量不存在")
            values["secret"] = os.environ[args.secret_env]
        data = client.request("/api/network/config", values) if values else client.request("/api/network/config")
    elif action in ("discover", "recover"):
        data = client.request("/api/network/" + action, {})
    elif action == "cancel":
        data = client.request("/api/network/cancel", {"run_id": args.run_id})
    elif action == "apply":
        print("正在切换最优节点并验证官方连接；验证失败会恢复切换前的节点。", file=sys.stderr)
        data = client.request("/api/network/apply", {"run_id": args.run_id} if args.run_id else {})
        if not args.no_wait:
            application_id = data["application"]["id"]
            data, code = wait_result(client, "network", data["id"], args.wait_timeout)
            # Another caller may apply the same ranking later; don't report its
            # success as the outcome of this CLI invocation.
            if not code:
                code = 0 if data.get("application", {}).get("id") == application_id and data["application"]["status"] == "succeeded" else 3
    elif action == "results":
        if args.watch and not args.run_id:
            raise ValueError("--watch 需要批次 ID")
        if args.watch:
            data, code = wait_result(client, "network", args.run_id, args.wait_timeout)
        else:
            data = read_result(client, "network", args.run_id) if args.run_id else client.request("/api/network/history?page=" + str(args.page))
    else:
        body = {key: getattr(args, key) for key in ("repeats", "top", "timeout", "nodes", "mode", "model", "sort_by", "concurrency") if getattr(args, key) is not None}
        if args.mode == "model" and not args.model:
            raise ValueError("模型实测需要 --model，使用本站登录的 ChatGPT 额度")
        if args.mode == "model":
            print("模型实测将使用 ChatGPT 额度；固定长输出题干、medium 推理，各节点并发测试。", file=sys.stderr)
        print("测试期间临时切换节点，影响新连接；结束或取消后恢复原选择。", file=sys.stderr)
        data = client.request("/api/network/run", body)
        print("批次: " + data["id"], file=sys.stderr)
        if not args.no_wait:
            data, code = wait_result(client, "network", data["id"], args.wait_timeout)
    emit(data, args.json)
    return code


def execute(args, client):
    """通过已有 API 配置站点、提交和查询任务，不另起模型调用。

    Args:
        args: parser 已校验的命令参数；敏感值只从环境或 stdin 读取。
        client: 经本机运行记录校验的 API 客户端。
    Returns:
        命令退出码，区分提交成功、完整测评和运行失败。
    """
    command = args.command
    if command == "vpn":
        return execute_vpn(args, client)
    if command == "sites":
        if args.action == "list":
            data = {"sites": client.request("/api/config")["sites"]}
        else:
            data = {"action": args.action, "name": args.name}
            if args.action == "set":
                data.update({k: getattr(args, k) for k in ("base_url", "models") if getattr(args, k) is not None})
                if args.api_key_env:
                    if not os.environ.get(args.api_key_env):
                        raise ValueError("指定的密钥环境变量不存在或为空")
                    data["api_key"] = os.environ[args.api_key_env]
                elif args.api_key_stdin:
                    data["api_key"] = sys.stdin.read(65537).strip()
                    if not data["api_key"] or len(data["api_key"]) > 65536:
                        raise ValueError("密钥为空或超过 64 KiB")
            data = client.request("/api/cli/site", data)
    elif command == "models":
        # Explicit model discovery must bypass the short cache used by automatic page loads.
        data = client.request("/api/models?" + urllib.parse.urlencode({"site": args.site, "refresh": "1"}))
        if data.get("error"):
            emit(data, args.json)
            return 3
    elif command == "results":
        if args.watch and not args.run_id:
            raise ValueError("--watch 需要批次 ID")
        if args.run_id and args.watch:
            data, code = wait_result(client, args.kind, args.run_id, args.wait_timeout, args.full)
            emit(data, args.json)
            return code
        history = {"performance": "/api/probe/history", "capacity": "/api/probe/history", "iq": "/api/iq/runs",
                   "engineering": "/api/swe/runs", "svg": "/api/runs"}
        data = read_result(client, args.kind, args.run_id, args.full) if args.run_id else client.request(history[args.kind])
    elif command == "cancel":
        endpoint = "/api/probe/cancel" if args.kind in ("performance", "capacity") else f'/api/{"swe" if args.kind == "engineering" else "iq"}/cancel'
        data = client.request(endpoint, {"run_id": args.run_id})
    else:
        if command == "retry":
            data = client.request("/api/iq/retry", {"run_id": args.run_id, "request_id": uuid.uuid4().hex})
        else:
            choices = select_targets(args, client)
            body = {"tasks": choices}
            if args.kind in ("performance", "capacity"):
                endpoint = "/api/probe"
                body = {"targets": choices, "mode": args.kind, "require_new": True}
            elif args.kind == "svg":
                endpoint = "/api/run"
            else:
                endpoint = f'/api/{"swe" if args.kind == "engineering" else "iq"}/run'
                body["request_id"] = uuid.uuid4().hex
            for key in ("concurrency", "suite", "repeats", "max_tokens", "request_timeout", "thinking_mode", "task_timeout", "step_limit", "instance_ids"):
                value = getattr(args, key, None)
                if value is not None:
                    body["timeout_seconds" if key == "request_timeout" and args.kind == "iq" else key] = value
            data = client.request(endpoint, body)
        run_id = data.get("run_id") or data["id"]
        print(f"批次: {run_id}；结果已保存在本机，网页也可查看。", file=sys.stderr)
        if not args.no_wait:
            data, code = wait_result(client, args.kind, run_id, args.wait_timeout)
            emit(data, args.json)
            return code
    emit(data, args.json)
    return 0


def logs(lines, follow):
    """流式读取本数据目录日志；不加载整个文件，Ctrl+C 只结束查看。"""
    if not 1 <= lines <= 10000:
        raise ValueError("--lines 必须在 1 到 10000 之间")
    path = DATA_DIR / "server.log"
    if not path.exists():
        raise ValueError("暂无后台日志；前台启动的日志在启动终端中")
    with path.open(encoding="utf-8", errors="replace") as source:
        print("".join(deque(source, maxlen=lines)), end="")
        while follow:
            line = source.readline()
            if line:
                print(line, end="", flush=True)
            else:
                time.sleep(.2)
