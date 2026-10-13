"""命令行控制台；复用本机服务的配置、任务和判分，不另建测评执行器。"""
from __future__ import annotations

import argparse
from collections import deque
import json
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


def emit(value, as_json=False):
    """JSON 输出保持可解析；文本输出把模型指标按行展示，不省略错误信息。"""
    if as_json or not isinstance(value, dict):
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    common = ("state", "id", "run_id", "status", "url", "data_directory", "log", "message")
    for key in common:
        if key in value:
            print(f"{key}: {value[key]}")
    if "rows" in value:
        print("站点 / 模型\t状态\t端到端 token/s\t首字秒数\t容量信号")
        for row in value["rows"]:
            metrics = row.get("metrics") or {}
            print(f'{row.get("site", "")} / {row.get("model", "")}\t{row.get("status", "")}\t'
                  f'{metrics.get("end_to_end_tokens_per_second", "-")}\t{metrics.get("ttft_seconds", "-")}\t'
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
    cmd.add_argument("page", nargs="?", default="performance", choices=("performance", "iq", "home", "settings", "engineering"))
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
    prefix = ("/api/probe/history/" if kind in ("performance", "capacity") else
              "/api/run/" if kind == "svg" else f'/api/{"swe" if kind == "engineering" else "iq"}/run/')
    return prefix + urllib.parse.quote(run_id, safe="") + ("?full=1" if full else "")


def pending(kind, data):
    """IQ 的回答完成不代表最终分可用；仍要等同一批次的自动复核结束。"""
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
    """进度写 stderr，stdout 留给最终结果；中断或等待超时都保留服务中的原任务。"""
    started, last, missing_review = time.monotonic(), None, None
    try:
        while True:
            data = client.request(result_path(kind, run_id, full))
            review = data.get("auto_review") or {}
            rows = data.get("rows", [])
            done = data.get("done", sum(r.get("status") not in ("queued", "running") for r in rows))
            text = f'{run_id}  {data.get("status", "running" if data.get("running") else "completed")}  {done}/{data.get("total", len(rows))}'
            if review:
                text += f'  复核 {review.get("status")} {review.get("done", 0)}/{review.get("total", 0)}'
            if data.get("auto_retry", {}).get("round"):
                text += f'  自动重试第 {data["auto_retry"]["round"]} 轮'
            if data.get("phase"):
                text += "  " + data["phase"]
            if text != last:
                print(text, file=sys.stderr, flush=True)
                last = text
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


def execute(args, client):
    """通过已有 API 配置站点、提交和查询任务；退出码明确区分提交成功与完整测评。"""
    command = args.command
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
        data = client.request("/api/models?" + urllib.parse.urlencode({"site": args.site}))
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
        data = client.request(result_path(args.kind, args.run_id, args.full) if args.run_id else history[args.kind])
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
