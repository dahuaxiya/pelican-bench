"""验证当前连接上的模型可用性与速度；附加本机诊断不改变账号或网络。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

import codex_channel
import codex_stream
import config
import llm
import svg_sanitize
from codex_probe import PERFORMANCE_PROMPT, MODES, _failure_category, performance_metrics
from network_probe import save_json

PROTOCOL = "codex-health-v3"
TIMEOUT = MODES["performance"]["timeout"]
SVG_LIMIT = 128 * 1024
DOCTOR_TIMEOUT = 90
SAMPLE_PAUSE = 2
ENVIRONMENTS = {"company", "home", "other"}
CHECKS = {
    "installation": "Codex CLI",
    "runtime.provenance": "CLI 运行环境",
    "config.load": "本机 CLI 配置",
    "auth.credentials": "本机 CLI 登录",
    "desktop.app.version": "桌面客户端",
    "desktop.app_server.handshake": "桌面进程通信",
    "app_server.status": "本机 App Server",
    "network.env": "本机 CLI 代理环境",
    "network.provider_reachability": "本机 CLI HTTP 可达性",
    "network.websocket_reachability": "本机 CLI WebSocket 握手",
    "state.paths": "本机状态与存储",
    "system.disk": "磁盘空间",
}
STATES = {"ok", "warning", "fail", "skipped"}
ADVICE = {
    "installation": "检查 Codex CLI 是否已安装、可执行，再刷新体检。",
    "runtime.provenance": "检查 CLI 安装来源与当前启动版本是否一致。",
    "config.load": "检查本机 Codex 配置语法及模型提供方；本站模型实测固定使用官方渠道。",
    "auth.credentials": "检查本机 Codex 登录是否有效；本站登录与本机登录可能不同。",
    "desktop.app.version": "检查桌面客户端是否可用；此项异常不等于官方模型容量不足。",
    "desktop.app_server.handshake": "检查桌面客户端的本机进程通信；保存工作后再考虑重启客户端。",
    "app_server.status": "检查 Codex 本机服务状态，不要据此判断远端模型容量。",
    "network.env": "核对系统代理与进程代理；未设置显式代理仍可能经过 VPN/TUN。",
    "network.provider_reachability": "核对代理、DNS、证书与出口连接；保持模型不变，换网络再测作对照。",
    "network.websocket_reachability": "核对代理是否支持 WebSocket；HTTP 可达不能替代握手。不要仅凭此项认定容量错误由网络引起。",
    "state.paths": "检查 Codex 状态目录权限与存储健康，不要直接删除会话数据。",
    "system.disk": "检查剩余磁盘空间，先备份再清理不需要的文件。",
}


def route_context():
    """记录两条执行路径的代理摘要，不保存用户名、密码、查询串或完整环境变量。

    Returns:
        本机 CLI 与本站实际子进程的代理设置；没有显式代理不代表没有 TUN/VPN。
    """
    def summarize(environment):
        """仅保留代理协议和主机端口；非法或自定义代理值不回显。

        Args:
            environment: 本地读取的进程环境，不接受前端传入。
        Returns:
            无凭据的代理摘要与 bypass 是否存在。
        """
        output = {}
        for key in ("http_proxy", "https_proxy", "all_proxy"):
            value = environment.get(key) or environment.get(key.upper())
            if value:
                try:
                    url = urlsplit(value)
                    host = url.hostname or ""
                    if url.scheme not in {"http", "https", "socks5", "socks5h", "socks4"} or not re.fullmatch(r"[a-zA-Z0-9.:-]{1,253}", host):
                        raise ValueError()
                    output[key] = f"{url.scheme}://{host}" + (f":{url.port}" if url.port else "")
                except ValueError:
                    output[key] = "已设置（不展示地址）"
        output["bypass_configured"] = bool(environment.get("no_proxy") or environment.get("NO_PROXY"))
        return output
    return {"cli": summarize(os.environ), "probe": summarize(codex_channel._environment())}


def sanitize_doctor(report):
    """只取固定检查、状态和已知诊断信号，禁止把 doctor 原文或凭据写入历史。

    Args:
        report: CLI 返回的 JSON；未知版本或结构返回缺项，不推断为正常。
    Returns:
        有界的检查列表和 CLI 版本。
    """
    if not isinstance(report, dict) or not isinstance(report.get("checks"), (list, dict)):
        raise ValueError("invalid doctor report")
    # CLI 0.151 returns an array; 0.161 keys the same checks by ID. Normalize only
    # these supported containers, so schema drift cannot silently become a pass.
    if isinstance(report["checks"], dict):
        indexed = {key: item for key, item in report["checks"].items() if isinstance(item, dict)}
    else:
        indexed = {item.get("id"): item for item in report["checks"]
                   if isinstance(item, dict) and isinstance(item.get("id"), str)}
    rows = []
    for key, title in CHECKS.items():
        item = indexed.get(key, {})
        status = item.get("status")
        # 配置详情会出现 WebSocket/代理等关键词，不能据此把配置告警误判为网络故障。
        # 只有网络检查的 summary/notes 可进入错误分类，其他项保留诊断状态和对应建议。
        text = json.dumps({key: item.get(key) for key in ("summary", "notes")}, ensure_ascii=True)
        diagnostic = ""
        if status in ("warning", "fail"):
            diagnostic = codex_channel._diagnostic(text) if key.startswith("network.") else "本机诊断报告告警" if status == "warning" else "本机诊断未通过"
        if "[unknown]" in diagnostic:
            diagnostic = "检查未通过，未取得可分类原因"
        details = item.get("details") or {}
        provider = details.get("model provider") if isinstance(details, dict) else None
        facts = []
        if isinstance(details, dict):
            for field, translations in {
                "respect system proxy": {"enabled": "CLI 跟随系统代理", "disabled": "CLI 不跟随系统代理"},
                "system proxy": {"manual": "系统配置了手动代理", "none": "未配置系统代理"},
            }.items():
                value = details.get(field)
                if isinstance(value, str) and value in translations:
                    facts.append(translations[value])
        rows.append({"id": key, "title": title, "status": status if isinstance(status, str) and status in STATES else "unknown",
                     "reason": diagnostic, "duration_ms": item.get("durationMs") if type(item.get("durationMs")) is int else None,
                     "facts": facts,
                     "advice": ADVICE[key] if status != "ok" else "",
                     "provider": "openai" if provider == "openai" else "other" if provider else None})
    version = str(report.get("codexVersion", ""))
    return {"version": version if re.fullmatch(r"[a-zA-Z0-9.+-]{1,64}", version) else "unknown", "checks": rows}


def doctor(stop):
    """运行本机 CLI 自带诊断；无推理请求，受总时限和取消信号约束。

    Args:
        stop: 本次检查的取消事件，不能终止用户的其他 Codex 进程。
    Returns:
        过滤后的网络、登录及安装诊断；不读取聊天记录或认证文件。
    """
    binary = codex_channel.binary_path()
    if not binary:
        return {"error": "未安装 Codex CLI", "checks": []}
    # 不套用本站的代理适配，保留本机 CLI 配置路径作为对照；不声称复现桌面会话。
    with tempfile.TemporaryDirectory(prefix="pelican-health-") as cwd, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([binary, "doctor", "--summary", "--json"], cwd=cwd,
                                   stdout=output, stderr=errors, start_new_session=True)
        deadline = time.monotonic() + DOCTOR_TIMEOUT
        try:
            while process.poll() is None:
                if stop.wait(.1):
                    raise llm.LLMCancelled("检查已停止")
                if output.tell() > 1024 * 1024 or errors.tell() > 1024 * 1024:
                    return {"error": "CLI 诊断输出超限", "checks": []}
                if time.monotonic() >= deadline:
                    return {"error": "CLI 诊断超时，不能据此判断模型容量", "checks": []}
            if output.tell() > 1024 * 1024:
                return {"error": "CLI 诊断输出超限", "checks": []}
            output.seek(0)
            try:
                return sanitize_doctor(json.load(output))
            except (ValueError, TypeError):
                return {"error": "此 CLI 未提供兼容的诊断结果", "checks": []}
        finally:
            # doctor 可能以非零退出码报告检查失败，仍解析 JSON；只回收本次进程组。
            if process.poll() is None:
                codex_channel._kill(process)


def verdict(run):
    """根据观测分层得出结论，握手成功或 HTTP 可达不能证明模型有容量。

    Args:
        run: 当前检查快照；文字测速样本与 SVG 检查分开记录。
    Returns:
        固定结论码、文案和样本数；不推断公司网络是容量错误的原因。
    """
    samples = list(run.get("samples", []))
    svg = run.get("svg_result") or {}
    if run.get("svg_check") and (svg.get("status") in {"success", "failed"}
            or (svg.get("status") == "cancelled" and svg.get("started_at"))):
        samples.append(svg)
    planned = run["repeats"] + int(bool(run.get("svg_check")))
    capacity = sum(bool(row.get("capacity_detected")) for row in samples)
    success = sum(row["status"] == "success" for row in samples)
    if capacity:
        code, text = "capacity", "观察到模型容量不足" + ("，部分请求已恢复" if success else "")
    elif any("[quota]" in row.get("reason", "") for row in samples):
        code, text = "quota", "模型请求受账号额度限制"
    elif run.get("error"):
        code, text = "inconclusive", "检查未完成，容量原因未确认"
    elif samples and success == len(samples) and len(samples) == planned:
        code, text = "passed", "文字测速与 SVG 生成均通过，未见容量不足" if run.get("svg_check") else "本轮模型请求通过，未见容量不足"
    elif run.get("svg_check") and svg.get("status") == "failed" and success:
        code, text = "inconclusive", "模型可连接，SVG 生成未通过"
    elif run.get("svg_check") and svg.get("status") in {"queued", "running"} and success == len(samples) and samples:
        code, text = "pending", "文字测速已完成，等待 SVG 生成结果"
    elif samples:
        code, text = "inconclusive", "模型请求未全部通过，容量原因未确认"
    else:
        code, text = "not_tested", "尚未进行模型实测，容量未知"
    return {"code": code, "text": text, "success": success, "capacity": capacity, "attempted": len(samples)}


def overview(run):
    """汇总体检项并给出有限建议，不用虚构健康分或把跳过项算通过。

    Args:
        run: 固定检查与模型样本，不读取原始诊断或账号信息。
    Returns:
        通过、待处理、未确认数量及建议；容量判断独立保留。
    """
    rows = (run.get("doctor") or {}).get("checks", [])
    passed = sum(row["status"] == "ok" for row in rows)
    issues = sum(row["status"] in {"warning", "fail"} for row in rows)
    # 只统计本轮选中的检查。默认模型测速不把未选择的12项诊断算成未知或失败。
    diagnostic_total = len(CHECKS) if run.get("diagnostics", True) else 0
    unknown = max(0, diagnostic_total - passed - issues)
    advice = [row["title"] + "：" + row["advice"] for row in rows if row.get("advice") and row["status"] in {"warning", "fail"}]
    # 文字与 SVG 各算一个检查项，不能因 SVG 失败把已通过的文字测速再算失败。
    model = verdict({**run, "svg_check": False})
    if model["code"] == "passed":
        passed += 1
    elif model["code"] in {"capacity", "quota"} or any(row["status"] == "failed" for row in run["samples"]):
        issues += 1
        if model["code"] == "capacity":
            advice.append("收到明确的模型容量提示。保持账号和模型一致，稍后或换网络复测；单次结果不能确定公司网络是原因。")
        elif model["code"] == "quota":
            advice.append("请求受账号额度限制，先检查用量与重置时间，切换节点不能据此保证恢复。")
        else:
            advice.append("模型请求失败但未确认容量原因，查看逐次错误类别，再核对登录、限流或连接问题。")
    else:
        unknown += 1
    if run.get("model_blocked"):
        advice.append(run["model_blocked"])
    if run.get("svg_check"):
        svg = run.get("svg_result") or {}
        if svg.get("status") == "success":
            passed += 1
        elif svg.get("status") == "failed":
            issues += 1
            advice.append("SVG 生成未通过：" + svg.get("reason", "查看生成结果与错误类别。"))
        else:
            unknown += 1
    return {"passed": passed, "issues": issues, "unknown": unknown,
            "total": diagnostic_total + 1 + int(bool(run.get("svg_check"))), "advice": advice}


def checked_svg(text):
    """抽取并清洗有界 SVG，验证结构；不评价画面美观或是否准确表达题意。

    Args:
        text: 模型的完整回答；原始回答不会进入健康检查历史。
    Returns:
        清洗后的 SVG、清理说明及动画标记；无有效图形时抛出固定文案。
    """
    source = svg_sanitize.extract_svg(text)
    if not source:
        raise ValueError("模型未返回完整 SVG")
    if len(source.encode("utf-8")) > SVG_LIMIT:
        raise ValueError("SVG 超过 128 KiB，未保存或预览")
    # 不解析声明或实体。清洗只负责移除危险构造，成功还必须满足 XML 结构。
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", source, re.IGNORECASE):
        raise ValueError("SVG 含不支持的文档声明")
    cleaned, notes = svg_sanitize.sanitize(source)
    try:
        root = ElementTree.fromstring(cleaned)
    except ElementTree.ParseError:
        raise ValueError("SVG 结构不完整或格式错误") from None
    if root.tag not in ("svg", "{http://www.w3.org/2000/svg}svg"):
        raise ValueError("SVG 根元素无效")
    shapes = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "use"}
    if not any(element.tag.split("}")[-1] in shapes for element in root.iter()):
        raise ValueError("SVG 中没有可展示的图形元素")
    if root.tag == "svg":
        # HTML 内联 SVG 可省略命名空间，独立图片文档则需要；只补这一项，不重新求图。
        root.set("xmlns", "http://www.w3.org/2000/svg")
        cleaned = ElementTree.tostring(root, encoding="unicode")
    if len(cleaned.encode("utf-8")) > SVG_LIMIT:
        raise ValueError("SVG 超过 128 KiB，未保存或预览")
    return {"svg": cleaned, "svg_bytes": len(cleaned.encode("utf-8")), "sanitize_notes": notes,
            "animated": svg_sanitize.has_animation(cleaned)}


class HealthChecks:
    """一次只运行一批；完整历史按 ID 保存，刷新页面不会创建请求。"""

    def __init__(self, root):
        """仅配置存储位置，导入模块不恢复或发起网络请求。

        Args:
            root: 服务端固定目录，测试必须提供临时目录。
        """
        self.root = Path(root)
        self.lock = threading.RLock()
        self.current = None
        self.stop = threading.Event()
        self.thread = None

    def busy(self):
        """只有本进程尚未收尾的工作占用运行槽，磁盘 running 记录视为已中断。"""
        with self.lock:
            return bool(self.current and self.current["status"] in {"running", "stopping"})

    def snapshot(self):
        """返回当前批次副本；没有当前工作时由前端从历史选择，不自动重测。"""
        with self.lock:
            return {"current": copy.deepcopy(self.current), "busy": self.busy()}

    def get(self, run_id):
        """按受限 ID 读取，重启遗留任务只显示中断、不自动消耗额度。

        Args:
            run_id: 32位十六进制检查 ID。
        Returns:
            独立快照或 None。
        """
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("无效的检查 ID")
        with self.lock:
            if self.current and self.current["id"] == run_id:
                return copy.deepcopy(self.current)
            try:
                row = json.loads((self.root / (run_id + ".json")).read_text())
                if not isinstance(row, dict) or row.get("id") != run_id:
                    return None
                if row["status"] in {"running", "stopping"}:
                    row.update(status="interrupted", phase="服务重启，未自动重发")
                    if (row.get("svg_result") or {}).get("status") in {"queued", "running"}:
                        row["svg_result"].update(status="interrupted", reason="服务重启，未自动重发")
                return row
            except (OSError, ValueError, KeyError):
                return None

    def history(self):
        """返回最多50个近期摘要；SVG 内容只在单条记录返回，列表不复制图片。"""
        paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        rows = []
        for path in paths[:50]:
            if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                continue
            row = self.get(path.stem)
            if row:
                rows.append({key: row.get(key) for key in ("id", "environment", "model", "model_check", "repeats",
                             "started_at", "finished_at", "status", "verdict", "protocol", "sample_mode", "diagnostics", "svg_check")})
        return {"runs": rows, "total": len(paths)}

    def _save(self):
        """持锁原子保存进度；初始保存失败不能发起收费请求，运行中失败明确告知。"""
        self.current["verdict"] = verdict(self.current)
        self.current["overview"] = overview(self.current)
        save_json(self.root / (self.current["id"] + ".json"), self.current)

    def start(self, request):
        """验证输入并幂等启动，原请求重发不能创建第二批收费任务。

        Args:
            request: 固定字段的检查参数；diagnostics 可选，旧客户端省略时保留本机诊断。
                svg_check 默认关闭以兼容旧客户端，网页显式开启时只增加一次 SVG 请求。
                不允许传提示词、命令、代理或认证，不能同时关闭模型实测与本机诊断。
        Returns:
            新建或已有的检查快照。
        """
        required = {"request_id", "environment", "model", "repeats", "model_check"}
        if not isinstance(request, dict) or not required <= set(request) or set(request) - required - {"diagnostics", "svg_check"}:
            raise ValueError("需要检查 ID、环境标签、模型、次数和模型实测开关")
        if (not isinstance(request["environment"], str) or request["environment"] not in ENVIRONMENTS or type(request["model_check"]) is not bool
                or type(request["repeats"]) is not int or not 1 <= request["repeats"] <= 3
                or type(request.get("diagnostics", True)) is not bool
                or type(request.get("svg_check", False)) is not bool
                or (request.get("svg_check") and not request["model_check"])
                or not (request["model_check"] or request.get("diagnostics", True))
                or not isinstance(request["model"], str)
                or not (codex_channel._MODEL.fullmatch(request["model"]) or request["model"] == "")):
            raise ValueError("检查参数无效；最多3次文字测速和1次 SVG 生成，SVG 需同时启用模型实测")
        with self.lock:
            previous = self.get(request["request_id"])
            if previous:
                if previous.get("request") != request:
                    raise ValueError("同一请求 ID 不能修改参数")
                return previous
            if self.busy():
                raise ValueError("已有健康检查在运行")
        catalog = codex_channel.catalog()
        if request["model_check"] and catalog.get("ready") and request["model"] not in catalog.get("models", []):
            raise ValueError("请选择官方目录中的模型")
        with self.lock:
            # 目录查询期间可能有另一请求建立任务，必须在登记前再次检查。
            previous = self.get(request["request_id"])
            if previous:
                if previous.get("request") != request:
                    raise ValueError("同一请求 ID 不能修改参数")
                return previous
            if self.busy():
                raise ValueError("已有健康检查在运行")
            old = self.current
            self.stop = threading.Event()
            self.current = {"id": request["request_id"], "request": copy.deepcopy(request),
                            **{k: request[k] for k in ("environment", "model", "repeats", "model_check")},
                            "protocol": PROTOCOL, "sample_mode": "performance",
                            "svg_check": request.get("svg_check", False),
                            "svg_result": {"status": "queued"} if request.get("svg_check") else None,
                            "diagnostics": request.get("diagnostics", True), "status": "running", "phase": "准备模型请求",
                            "started_at": time.time(), "finished_at": None, "samples": [],
                            "route": route_context(), "auth_source": catalog.get("auth_source", "unknown"),
                            "execution": {"reasoning_effort": "medium", "timeout_seconds": TIMEOUT,
                                          "prompt_profile": "network-explanation-600-800-words",
                                          "svg_prompt_profile": "pelican-cycle-svg-v1" if request.get("svg_check") else None},
                            "cli_version": catalog.get("cli_version", "unknown"), "doctor": None}
            if request["model_check"] and not catalog.get("ready"):
                # 未登录本身是体检结果，不应阻止安装、配置和网络检查。
                self.current["model_blocked"] = "本站官方渠道未就绪，模型实测已跳过。请在设置中核对登录与模型目录。"
            try:
                # 必须先持久化再发送请求；磁盘或启动失败不能留下永远占用的运行槽。
                self._save()
                self.thread = threading.Thread(target=self._run, name="codex-health-check", daemon=True)
                self.thread.start()
            except Exception:
                self.current = old
                raise ValueError("检查未启动，请检查本地存储或服务状态") from None
            return copy.deepcopy(self.current)

    def cancel(self, run_id=None):
        """停止指定批次；旧标签页不能误停新任务。服务退出可省略 ID。

        Args:
            run_id: 页面正在显示的检查 ID，None 仅供服务内部清理。
        Returns:
            当前批次快照；不清理历史或更改代理。
        """
        with self.lock:
            if run_id is not None and (not self.current or run_id != self.current["id"]):
                raise ValueError("该检查不是当前任务，未停止其他检查")
            if self.busy():
                self.current.update(status="stopping", phase="正在停止检查")
                self.stop.set()
            return copy.deepcopy(self.current)

    def close(self):
        """取消并等待本次子进程清理，退出服务不遗留推理请求。"""
        self.cancel()
        if self.thread:
            self.thread.join(max(TIMEOUT, DOCTOR_TIMEOUT) + 5)

    def _run_svg(self):
        """额外执行一次 SVG 生成；先保存状态，取消和账号阻塞均不继续发请求。

        图片通过清洗和结构校验后才保存，正文不作为 HTML 插入页面；
        指标与文字测速分开，防止不同提示词的耗时被混为同一组样本。
        """
        with self.lock:
            if not self.current["svg_check"]:
                return
            blocking = self.current.get("model_blocked") or next((
                row.get("reason") for row in self.current["samples"]
                if any(code in row.get("reason", "") for code in ("[quota]", "[authentication]"))), None)
            if self.stop.is_set() or blocking:
                self.current["svg_result"] = {
                    "status": "cancelled" if self.stop.is_set() else "skipped",
                    "reason": "检查已停止" if self.stop.is_set() else "账号未就绪或受限，未发送 SVG 请求"}
                self._save()
                return
            sample = {"status": "running", "events": [], "capacity_detected": False,
                      "started_at": time.time()}
            self.current.update(phase="正在生成 SVG", svg_result=copy.deepcopy(sample))
            self._save()
        started = time.monotonic()

        def observe(event):
            """只收适配器安全分类；不把模型正文或原始错误复制到诊断。

            Args:
                event: 适配器内部生成的无凭据诊断事件。
            """
            sample["events"] = (sample["events"] + [event])[-40:]
            sample["capacity_detected"] |= "[capacity]" in event.get("category", "")

        try:
            response = codex_stream.complete(self.current["model"], config.DEFAULT_PROMPT, TIMEOUT,
                                             self.stop, on_diagnostic=observe)
            sample["capacity_detected"] |= (response.get("transport_diagnostics") or {}).get("capacity_detected", False)
            sample["transport"] = response.get("transport", "codex-cli")
            # 图形格式失败不抹去真实请求时间，但只有通过验证的图形才展示吞吐成绩。
            sample["metrics"] = performance_metrics(response, time.monotonic() - started, "capacity")
            if not response.get("completed") or response.get("error") or response.get("truncated"):
                raise ValueError("SVG 响应未完整结束")
            sample.update(checked_svg(response.get("text") or ""), status="success")
            sample["metrics"] = performance_metrics(response, time.monotonic() - started, "performance")
        except llm.LLMCancelled:
            sample.update(status="cancelled", reason="检查已停止")
        except llm.LLMError as exc:
            sample.update(status="failed", reason=str(exc) if isinstance(exc, codex_channel.CodexRequestError)
                          else _failure_category(str(exc)))
            sample["capacity_detected"] |= "[capacity]" in sample["reason"]
            if isinstance(exc, codex_channel.CodexRequestError):
                sample["metrics"] = {"ttft_seconds": exc.ttft}
                sample["capacity_detected"] |= exc.transport_diagnostics.get("capacity_detected", False)
        except ValueError as exc:
            # 上述校验只抛固定文案，不回显解析器报错和原始模型正文。
            sample.update(status="failed", reason=str(exc))
        sample["duration_seconds"] = round(time.monotonic() - started, 3)
        with self.lock:
            self.current["svg_result"] = sample
            self._save()

    def _run(self):
        """依次完成文字测速、可选 SVG 生成和本机诊断；不保存文字测速正文。

        与性能页复用固定长输出及指标口径，避免用 OK 短回答制造吞吐数据。
        模型失败不触发额外自动重试，取消后不再启动本机诊断。
        """
        try:
            if self.current["model_check"] and not self.current.get("model_blocked"):
                for index in range(self.current["repeats"]):
                    if self.stop.is_set():
                        break
                    with self.lock:
                        self.current["phase"] = f"模型实测 {index + 1} / {self.current['repeats']}"
                        self._save()
                    sample = {"number": index + 1, "status": "failed", "events": [], "capacity_detected": False}
                    started = time.monotonic()
                    def observe(event):
                        """只保存适配器已分类的事件，恢复成功也不清除容量信号。

                        Args:
                            event: 适配器内部安全诊断，不含原始 stderr 或答案。
                        """
                        sample["events"] = (sample["events"] + [event])[-40:]
                        sample["capacity_detected"] |= "[capacity]" in event.get("category", "")
                    try:
                        response = codex_stream.complete(self.current["model"], PERFORMANCE_PROMPT, TIMEOUT, self.stop, on_diagnostic=observe)
                        sample["status"] = "success" if (response.get("completed") and not response.get("error")
                            and not response.get("truncated") and str(response.get("text") or "").strip()) else "failed"
                        sample["capacity_detected"] |= response.get("transport_diagnostics", {}).get("capacity_detected", False)
                        # 不完整或截断的响应不能贡献成功测速；保留时序供诊断，但不出吞吐。
                        sample["metrics"] = performance_metrics(response, time.monotonic() - started,
                            "performance" if sample["status"] == "success" else "capacity")
                        sample["transport"] = response.get("transport", "codex-cli")
                        if sample["status"] == "failed":
                            sample["reason"] = "响应未完整结束，容量未确认"
                    except llm.LLMCancelled:
                        sample.update(status="cancelled", reason="检查已停止")
                    except llm.LLMError as exc:
                        sample["reason"] = _failure_category(str(exc))
                        sample["capacity_detected"] |= "[capacity]" in sample["reason"]
                    sample["duration_seconds"] = round(time.monotonic() - started, 3)
                    with self.lock:
                        self.current["samples"].append(sample)
                        self._save()
                    # 诊断不是压力测试；次数有界且串行，遇到额度/认证失败立即停止。
                    if any(kind in sample.get("reason", "") for kind in ("[quota]", "[authentication]")):
                        break
                    if index + 1 < self.current["repeats"] and self.stop.wait(SAMPLE_PAUSE):
                        break
            self._run_svg()
            if self.current["diagnostics"] and not self.stop.is_set():
                # 本机诊断可能很慢且使用另一条配置路径，不能挡住已完成的模型测速结果。
                with self.lock:
                    self.current["phase"] = "补充本机诊断"
                    self._save()
                result = doctor(self.stop)
                with self.lock:
                    self.current["doctor"] = result
                    self._save()
        except llm.LLMCancelled:
            pass
        except Exception:
            with self.lock:
                self.current["error"] = "检查程序未能完成，不能判断容量原因"
        finally:
            with self.lock:
                try:
                    self.current["route_after"] = route_context()
                    self.current["route_changed"] = self.current["route_after"] != self.current["route"]
                except Exception:
                    self.current["route_changed"] = None
                self.current.update(status="cancelled" if self.stop.is_set() else "completed",
                                    phase="检查已结束", finished_at=time.time())
                if (self.current.get("svg_result") or {}).get("status") in {"queued", "running"}:
                    # 程序异常也要结束 SVG 子项，不能让已结束的历史仍显示“生成中”。
                    self.current["svg_result"].update(
                        status="cancelled" if self.stop.is_set() else "interrupted",
                        reason="检查已停止" if self.stop.is_set() else "检查中断，未取得 SVG 结果")
                if self.current.get("error"):
                    self.current["status"] = "failed"
                try:
                    self._save()
                except OSError:
                    self.current["error"] = "检查结果保存失败，请检查磁盘"
                    self.current["status"] = "failed"
                    self.current["verdict"] = verdict(self.current)


manager = HealthChecks(Path(config.ROOT) / ".local" / "health-checks")
