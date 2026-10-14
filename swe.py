"""独立工程榜：固定真实仓库任务、独立容器验收、最近完整有效成绩。

Web 进程不导入重型 Agent 依赖；每道题在专用 Python 子进程中执行。
密钥经 stdin 传入，不进入命令行、运行记录或任务容器。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import signal
import threading
import time
import uuid
import urllib.request
from typing import Any

import config
import iq
import llm
from runtime_paths import DISTRIBUTED
from query_cache import QueryCache, fingerprint

ROOT = Path(config.ROOT) / "swe-runs"
DATA = Path(config.ROOT) / "swe-data"
PYTHON = Path(config.ROOT) / ".venv-swe" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
DOCKER = shutil.which("docker") or "docker"
# 源码用户沿用已有专用环境；分发安装默认使用用户当前 Docker context。
CONTEXT = os.environ.get("PELICAN_BENCH_DOCKER_CONTEXT") or os.environ.get("DOCKER_CONTEXT") or ("default" if DISTRIBUTED else "colima-pelican-swe")
RUNNER_VERSION = "pelican-swe-v3"
VERSIONS = {"mini-swe-agent": "2.4.6", "swebench": "4.1.0"}
# 只排除已明确属于图像生成 API 的模型系列；未知文本模型仍允许实测，不能猜测其能力。
UNSUPPORTED_MODEL_PREFIXES = {"gpt-image-": "仅支持图像生成，不能参加工程评测"}
SUITES = {"smoke": ("SWE-bench Lite · 3 题离线试跑", ["pallets__flask-4045", "pallets__flask-4992", "pallets__flask-5063"]),
          "lite": ("SWE-bench Lite · 全部 300 题", None)}
_RUNS: dict[str, dict] = {}
_LOCK = threading.RLock()
ACTIVE_PORT = None
_HISTORY_CACHE = QueryCache(entries=128)
_ENV_LOCK = threading.Lock()
_ENV_CHECK = None


def digest(value: Any) -> str:
    """生成稳定条件摘要，字典顺序不会影响归组。

    @param value 可 JSON 序列化的公开条件
    @return SHA-256 十六进制串
    """
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def run_path(run_id: str) -> Path:
    """只允许服务端 UUID，拒绝让 URL 参与任意路径访问。

    @param run_id 32 位小写十六进制批次 ID
    @return 专用运行目录
    """
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("无效的工程评测 ID")
    return ROOT / run_id


def save(data: dict) -> None:
    """原子保存批次；调用方持有状态锁，写盘失败时不能继续发起模型请求。

    @param data 不含凭据的批次数据
    """
    target = run_path(data["run_id"])
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = target / "run.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False))
    temporary.replace(target / "run.json")


def catalog() -> dict:
    """只展示题集元数据，参考补丁和验收测试不发给浏览器或模型。

    @return 固定题集、执行器版本和数据准备状态
    """
    manifest = json.loads((DATA / "manifest.json").read_text()) if (DATA / "manifest.json").is_file() else None
    return {"suites": [{"id": key, "name": title, "count": len(ids) if ids else 300}
                       for key, (title, ids) in SUITES.items()], "dataset": manifest,
            "runner": RUNNER_VERSION, "versions": VERSIONS,
            "unsupported_model_prefixes": UNSUPPORTED_MODEL_PREFIXES}


def tasks_for(suite: str, instance_ids: list[str] | None = None) -> tuple[dict, list[dict]]:
    """读取并校验题集，损坏快照不能继续用于成绩比较。

    @param suite 固定 suite id
    @param instance_ids 可选的题目子集；只能选固定题集内的题目，不能为空或重复
    @return 条件元数据和所选真实任务
    """
    if suite not in SUITES:
        raise ValueError("未知工程题集")
    meta = catalog()["dataset"]
    if not meta:
        raise ValueError("尚未准备 SWE-bench 数据")
    body = (DATA / "tasks.json").read_bytes()
    if hashlib.sha256(body).hexdigest() != meta["sha256"]:
        raise ValueError("题集校验失败，请重新准备固定版本数据")
    all_tasks = json.loads(body)
    ids = SUITES[suite][1]
    tasks = [row for row in all_tasks if ids is None or row["instance_id"] in ids]
    if len(tasks) != (len(ids) if ids else 300):
        raise ValueError("题集不完整")
    if instance_ids is not None:
        if (not isinstance(instance_ids, list) or not instance_ids
                or not all(isinstance(value, str) for value in instance_ids)
                or len(set(instance_ids)) != len(instance_ids)
                or not set(instance_ids).issubset({row["instance_id"] for row in tasks})):
            raise ValueError("重测题目必须是所选任务集内不重复的题目列表")
        # 改超时后的失败项重测是独立子集；真实题目列表和数量参与分组，不能冒充完整题集成绩。
        tasks = [row for row in tasks if row["instance_id"] in instance_ids]
    return {**meta, "id": suite, "count": len(tasks), "instance_ids": [t["instance_id"] for t in tasks]}, tasks


def environment(refresh: bool = False) -> dict:
    """只检查依赖与专用 Docker，不因打开页面拉镜像或调用模型。

    @param refresh 创建任务时必须为 True；页面轮询只复用十秒内的依赖探测
    @return 是否可运行及具体缺失项，题集和磁盘空间始终实时读取
    """
    global _ENV_CHECK
    identity = (str(PYTHON), DOCKER, CONTEXT)
    with _ENV_LOCK:
        if refresh or _ENV_CHECK is None or _ENV_CHECK[0] != identity or time.monotonic() - _ENV_CHECK[1] >= 10:
            _ENV_CHECK = (identity, time.monotonic(), _environment_issues())
        issues = list(_ENV_CHECK[2])
    if not (DATA / "tasks.json").is_file():
        issues.append("SWE-bench Lite 题集未准备")
    if shutil.disk_usage(config.ROOT).free < 10 * 1024**3:
        issues.append("磁盘可用空间不足 10 GiB")
    return {"ready": not issues, "issues": issues, "context": CONTEXT, **catalog()}


def _environment_issues():
    """Run bounded dependency checks; callers share the result only for display, not task admission."""
    issues = []
    if not PYTHON.is_file():
        issues.append("缺少工程评测 Python 环境")
    else:
        try:
            check = subprocess.run([str(PYTHON), "-c", "import importlib.metadata as m,json; print(json.dumps({n:m.version(n) for n in ['mini-swe-agent','swebench']}))"],
                                   capture_output=True, text=True, timeout=10)
            if check.returncode or json.loads(check.stdout) != VERSIONS:
                issues.append("Agent 或判分器版本不匹配")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            issues.append("工程评测依赖无法读取")
    try:
        check = subprocess.run([DOCKER, "--context", CONTEXT, "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, text=True, timeout=8)
        if check.returncode:
            issues.append("专用 Docker 环境未启动")
    except (OSError, subprocess.TimeoutExpired):
        issues.append("专用 Docker 环境不可用")
    return issues


def summarize(data: dict) -> list[dict]:
    """完整任务集合才产生成绩；接口、环境失败与模型未修复分开。

    @param data 已保存的批次
    @return 每个站点模型的修复率、覆盖率与可比较条件
    """
    output = []
    total = data["suite"]["count"] * data["options"]["repeats"]
    for selected in data["configurations"]:
        rows = [row for row in data["results"] if (row["site"], row["model"]) == (selected["site"], selected["model"])]
        valid = len(rows) == total and all(row["status"] in ("resolved", "unresolved") for row in rows)
        resolved = sum(row["status"] == "resolved" for row in rows)
        # 镜像 digest 也属于条件：latest 镜像变化不能混入同一组历史。
        images = sorted({(row["instance_id"], row.get("image", "")) for row in rows})
        # 历史缺省按文本命令、180 秒、非流式解释；只补读取视图，不重写原文件。
        conditions = {"suite": data["suite"], "options": comparison_options(data["options"]), "runner": data["runner"],
                      "versions": data["versions"], "thinking_mode": selected["effective_thinking_mode"], "images": images}
        if selected.get("execution"):
            conditions["execution"] = selected["execution"]
            conditions["options"].update(max_tokens=None, temperature=None, request_retries=0,
                                          action_mode="text", stream=False, output_limit_policy="abort")
        output.append({**selected, "resolved": resolved, "completed": len(rows), "total": total,
                       "success_rate": round(resolved / total * 100, 2) if valid else None,
                       "valid": valid, "conditions": conditions, "cohort_id": digest(conditions),
                       "duration_seconds": round(sum(row.get("duration_seconds", 0) for row in rows), 1)})
    return output


def comparison_options(options: dict) -> dict:
    """补齐旧批次的真实缺省行为；重试及截断处理改变必须分榜，不能复用旧条件成绩。

    @param options 已持久化的批次预算，不修改原对象
    @return 用于分组和复用的完整预算，历史缺省重试次数为 0
    """
    return {"request_timeout": 180, "stream": False, "action_mode": "text", "request_retries": 0,
            "output_limit_policy": "abort", **options}


def get(run_id: str, full: bool = False) -> dict | None:
    """读取历史并保留重启中断；不会自动重新花费模型额度。

    @param run_id 固定批次 ID
    @param full 是否包含逐题补丁与测试日志
    @return 不含凭据的快照，不存在时为 None
    """
    target = run_path(run_id) / "run.json"
    with _LOCK:
        active = _RUNS.get(run_id)
        data = copy.deepcopy(active["data"]) if active else None
    if not active and target.is_file():
        def read():
            """Retain only fields needed by this view; full evidence still comes from the original."""
            saved = json.loads(target.read_text())
            if not full:
                saved["results"] = [{k: v for k, v in row.items() if k not in
                                     ("patch", "test_log", "trajectory", "response_diagnostics")}
                                    for row in saved["results"]]
            return saved
        # Reading history must not hold the execution lock while parsing a large trajectory.
        data = read() if full else _HISTORY_CACHE.get(str(target), fingerprint(target), read)
    if not data:
        return None
    if not active and data["status"] in ("running", "stopping"):
        if ACTIVE_PORT:
            # 热升级时旧进程仍持有任务；只回读精确 UUID，不能把它误标成重启中断。
            url = f"http://127.0.0.1:{ACTIVE_PORT}/api/swe/run/{run_id}" + ("?full=1" if full else "")
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=3) as response:
                    remote = json.load(response)
                if remote.get("run_id") == run_id:
                    remote["managed_by_url"] = f"http://127.0.0.1:{ACTIVE_PORT}/static/engineering.html"
                    return remote
            except (OSError, ValueError):
                data["phase"] = "原工程服务暂时不可达，任务状态未确认"
            data["managed_by_url"] = f"http://127.0.0.1:{ACTIVE_PORT}/static/engineering.html"
        else:
            data.update(status="interrupted", phase="服务已重启，未自动补跑")
    if active and data["status"] in ("running", "stopping"):
        progress = run_path(run_id) / str(data["done"]) / "progress.json"
        try:
            data["progress"] = json.loads(progress.read_text())
        except (OSError, ValueError):
            pass  # 切换题目时进度文件可能尚未创建，不沿用上一题的进度。
    data["summary"] = summarize(data)
    if not full:
        data["results"] = [{k: v for k, v in row.items() if k not in ("patch", "test_log", "trajectory", "response_diagnostics")}
                           for row in data["results"]]
    return data


def history() -> list[dict]:
    """遍历本功能批次，损坏文件跳过但不删除。

    @return 最近批次在前的公开摘要
    """
    rows = []
    if ROOT.is_dir():
        for entry in ROOT.iterdir():
            if re.fullmatch(r"[a-f0-9]{32}", entry.name):
                try:
                    data = get(entry.name)
                    if data:
                        rows.append(data)
                except (OSError, ValueError, KeyError):
                    continue
    return sorted(rows, key=lambda row: (row["started_at"], row["run_id"]), reverse=True)


def leaderboard(cohort: str | None = None) -> dict:
    """按实际条件取每个组合最新完整成绩，不等待同批其他模型完成。

    每个模型的全部题目和重复次数都通过完整性检查后才入榜；零分有效，
    其他模型仍运行、取消或失败不影响它，未完成或接口失败也不能替换旧成绩。

    @param cohort 可选条件摘要
    @return 独立工程榜，基础题成绩永远不参与
    """
    groups = {}
    for run in history():
        for row in run["summary"]:
            # 完整性以站点和模型组合为单位，由 summarize 校验题数、次数和逐题状态。
            # 跳过整个运行中批次会让已经验收完的模型等待队列里其他模型数小时。
            if not row["valid"]:
                continue
            group = groups.setdefault(row["cohort_id"], {"id": row["cohort_id"], "conditions": row["conditions"], "entries": {}})
            group["entries"].setdefault((row["site"], row["model"]), {**row, "run_id": run["run_id"], "started_at": run["started_at"]})
    selected = cohort or next(iter(groups), None)
    if selected and selected not in groups:
        raise ValueError("这组工程评测条件不存在，请刷新")
    entries = list(groups[selected]["entries"].values()) if selected else []
    entries.sort(key=lambda row: (-row["success_rate"], row["site"], row["model"]))
    return {"cohort_id": selected, "cohorts": [{"id": group["id"], "conditions": group["conditions"]} for group in groups.values()], "entries": entries}


def start(cfg: dict, request: dict) -> dict:
    """验证公开参数后启动串行任务，重试同一请求 ID 不会重复扣费。

    @param cfg 只在后台内存使用的站点配置
    新批次固定流式工具调用；命令协议进入比较条件，不复用旧文本命令成绩。
    @param request 模型、题集及可选子集、预算与请求 ID；request_timeout 控制每次模型请求，reuse_from 仅沿用同条件任务
    request_retries 控制同一步暂时失败的额外请求上限，不重复已成功的模型回复和命令。
    新批次的输出截断交由官方 Agent 格式纠错，仍计入原步数和时间预算。
    @return 已创建或幂等命中的批次
    """
    run_id = request.get("request_id")
    run_path(run_id)
    suite, tasks = tasks_for(request.get("suite", "smoke"), request.get("instance_ids"))
    options = {"repeats": iq._integer(request.get("repeats", 1), 1, 5, "重复次数"),
               "step_limit": iq._integer(request.get("step_limit", 80), 1, 250, "每题步数"),
               "max_tokens": iq._integer(request.get("max_tokens", 8192), 256, 262144, "每步输出上限"),
               "task_timeout": iq._integer(request.get("task_timeout", 1800), 60, 7200, "单题时间"),
               "request_timeout": iq._integer(request.get("request_timeout", 600), 30, 3600, "模型请求超时"),
               "request_retries": iq._integer(request.get("request_retries", 2), 0, 5, "单步请求重试次数"),
               "temperature": "provider_default", "stream": True, "action_mode": "tool_calls",
               "output_limit_policy": "agent_feedback"}
    choices = request.get("tasks")
    if not isinstance(choices, list) or not 1 <= len(choices) <= 100:
        raise ValueError("请选择 1 到 100 个站点模型组合")
    selected, seen = [], set()
    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get("model"), str):
            raise ValueError("模型格式错误")
        site = next((s for s in cfg["sites"] if s["name"] == choice.get("site")), None)
        model = choice["model"].strip()
        if not site or not site.get("base_url") or not model or len(model) > 200:
            raise ValueError("站点或模型无效")
        for prefix, reason in UNSUPPORTED_MODEL_PREFIXES.items():
            if model.lower().startswith(prefix):
                raise ValueError(f'{site["name"]} / {model}：{reason}')
        pair = (site["name"], model)
        if pair in seen:
            raise ValueError("模型组合不能重复")
        seen.add(pair)
        mode = site.get("thinking_mode", "default")
        selected.append({"site": site["name"], "model": model, "thinking_mode": mode,
                         "effective_thinking_mode": llm.effective_thinking_mode(model, mode)})
        import official_channels
        channel = official_channels.adapter(site["base_url"])
        if channel:
            execution = channel.execution(model)
            selected[-1].update(execution={**execution, "action_mode": "text"}, effective_thinking_mode=
                "codex-medium" if execution["transport"] == "codex-cli" else "kimi-default")
    reuse_from = request.get("reuse_from")
    if ACTIVE_PORT and any(row["status"] in ("running", "stopping") and row.get("managed_by_url") for row in history()):
        raise ValueError(f"原工程任务仍在运行，请先到 http://127.0.0.1:{ACTIVE_PORT}/static/engineering.html 处理")
    reused = reusable_results(reuse_from, suite, options, selected) if reuse_from else []
    fingerprint = digest([suite, options, selected] + ([reuse_from] if reuse_from else []))
    with _LOCK:
        previous = get(run_id)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ValueError("同一请求 ID 不可用于不同设置")
            return previous
        if any(run["data"]["status"] in ("running", "stopping") for run in _RUNS.values()):
            raise ValueError("已有工程评测运行中")
        ready = environment(refresh=True)
        if not ready["ready"]:
            raise ValueError("；".join(ready["issues"]))
        data = {"run_id": run_id, "fingerprint": fingerprint, "suite": suite, "options": options,
                "runner": RUNNER_VERSION, "versions": VERSIONS, "configurations": selected, "results": reused,
                "started_at": time.time(), "finished_at": None, "status": "running", "phase": "准备环境",
                "done": len(reused), "total": len(tasks) * len(selected) * options["repeats"], "error": "",
                "reuse_from": reuse_from}
        save(data)
        active = {"data": data, "stop": threading.Event()}
        _RUNS[run_id] = active
        threading.Thread(target=_execute, args=(active, copy.deepcopy(cfg), tasks), daemon=True).start()
    return get(run_id)


def reusable_results(source_id: str, suite: dict, options: dict, selected: list[dict]) -> list[dict]:
    """只从明确指定的已结束批次复用同题结果，不搜索历史高分，不复用接口或环境失败。

    题集因环境问题换题时，相同数据版本、预算、执行器和模型的已完成题无需再次计费。
    @param source_id 来源批次的服务端 UUID，不能指向正在运行的批次
    @param suite 新题集元数据与任务 ID
    @param options 新批次预算、传输和命令协议，必须与来源完全相同
    @param selected 新批次模型和实际推理参数
    @return 带来源批次 ID 的成绩副本，原始记录保持不变
    """
    source = get(source_id, full=True)
    if not source or source["status"] in ("running", "stopping"):
        raise ValueError("沿用结果需要指定已结束的工程批次")
    # 命令协议、超时和传输都会影响成功率；历史缺省值按当时行为解释，不跨条件复用。
    if (comparison_options(source["options"]) != comparison_options(options)
            or source["runner"] != RUNNER_VERSION or source["versions"] != VERSIONS
            or source["suite"]["sha256"] != suite["sha256"]):
        raise ValueError("来源批次的数据、预算或执行器不同，不能沿用")
    configurations = {(row["site"], row["model"]): row for row in source["configurations"]}
    wanted = {(row["site"], row["model"]): row for row in selected}
    output, seen = [], set()
    for row in source["results"]:
        pair = (row["site"], row["model"])
        key = (*pair, row["instance_id"], row["repeat"])
        if (pair not in wanted or configurations.get(pair) != wanted[pair]
                or row["instance_id"] not in suite["instance_ids"] or row["repeat"] > options["repeats"]
                or row["status"] not in ("resolved", "unresolved")):
            continue
        if key in seen:
            raise ValueError("来源批次同一任务有重复成绩，不能自动沿用")
        seen.add(key)
        output.append({**copy.deepcopy(row), "source_run_id": source_id})
    return output


def wait_worker(proc: subprocess.Popen, payload: dict, stop: threading.Event) -> bool:
    """等待单题并响应取消，不能让阻塞的模型调用把停止按钮拖到整题结束。

    @param proc 本题唯一 worker，凭据仅经 stdin 传递
    @param payload 私有任务参数
    @param stop 当前批次的停止事件
    @return 是否因主动取消而终止 worker；外层仍须回收本题容器
    """
    deadline = time.monotonic() + payload["options"]["task_timeout"] + 4200
    encoded = json.dumps(payload)
    while True:
        if stop.is_set() or time.monotonic() >= deadline:
            cancelled = stop.is_set()
            # worker 与其 Codex 子进程共用新进程组，停止后不能继续消耗账号额度。
            try:
                if os.getpgid(proc.pid) == proc.pid:
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()  # 兼容旧版 worker 或外部测试进程，不能杀调用者所在进程组。
            except ProcessLookupError:
                pass
            proc.communicate()
            return cancelled
        try:
            proc.communicate(encoded, timeout=1)
            return False
        except subprocess.TimeoutExpired:
            # communicate 会继续发送未写完的数据，后续不得重复传入密钥或任务内容。
            encoded = None


def _execute(active: dict, cfg: dict, tasks: list[dict]) -> None:
    """串行控制模型与容器预算，每题保存后再调度下一题；失败不假装成答错。

    @param active 本批状态与停止事件
    @param cfg 私有配置，仅经 stdin 送给固定 worker
    @param tasks 已校验的官方任务，参考补丁不放入模型上下文
    """
    data = active["data"]
    completed = {(r["site"], r["model"], r["instance_id"], r["repeat"]) for r in data["results"]}
    try:
        for repeat in range(1, data["options"]["repeats"] + 1):
            for task in tasks:
                for selected in data["configurations"]:
                    if (selected["site"], selected["model"], task["instance_id"], repeat) in completed:
                        continue  # 沿用成绩已经通过同条件校验，禁止为同一题重复发起付费请求。
                    with _LOCK:
                        if active["stop"].is_set():
                            return
                        if shutil.disk_usage(config.ROOT).free < 10 * 1024**3:
                            data["error"] = "磁盘可用空间不足 10 GiB，已停止后续任务"
                            return
                        index = data["done"]
                        work = run_path(data["run_id"]) / str(index)
                        work.mkdir(mode=0o700, exist_ok=True)
                        data["phase"] = f'{selected["site"]} / {selected["model"]} · {task["instance_id"]}'
                        save(data)
                        site = next(s for s in cfg["sites"] if s["name"] == selected["site"])
                        label = data["run_id"] + "-" + str(index)
                        payload = {"task": task, "selected": selected, "site": site,
                                   "options": data["options"], "work": str(work), "label": label}
                        # 所有参数由本进程生成；模型输出只能在 worker 的无网络容器执行。
                        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "CODEX_HOME",
                               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY") if k in os.environ}
                        env.update(DOCKER_CONTEXT=CONTEXT, LITELLM_LOCAL_MODEL_COST_MAP="True",
                                   MSWEA_GLOBAL_CONFIG_DIR=str(work / "agent-config"), MSWEA_SILENT_STARTUP="1")
                        # 子进程也必须选中同一数据目录，不能回落到只读 npm 安装目录。
                        env["PELICAN_BENCH_DATA_DIR"] = config.ROOT
                        proc = subprocess.Popen([str(PYTHON), str(Path(config.SOURCE_ROOT) / "swe_worker.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=config.ROOT, env=env, text=True, start_new_session=True)
                    try:
                        cancelled = wait_worker(proc, payload, active["stop"])
                    finally:
                        # 子进程被杀时 finally 不运行；宿主按本题 label 精确回收，
                        # 不留下仍能执行模型命令的容器，也不触碰其他项目资源。
                        cleanup = subprocess.run([DOCKER, "--context", CONTEXT, "ps", "-aq", "--filter", "label=pelican-swe=" + label],
                                                 capture_output=True, text=True, timeout=30)
                        ids = cleanup.stdout.split()
                        if ids:
                            subprocess.run([DOCKER, "--context", CONTEXT, "rm", "-f", *ids],
                                           capture_output=True, timeout=30, check=True)
                    result_file = work / "result.json"
                    if cancelled:
                        result = {"status": "cancelled", "reason": "用户停止了当前题，未计入成绩"}
                    elif result_file.is_file():
                        result = json.loads(iq._redact(result_file.read_text(), cfg))
                    else:
                        result = {"status": "environment_error", "reason": "执行器退出或超时，没有可信验收结果"}
                    result.update(site=selected["site"], model=selected["model"], instance_id=task["instance_id"], repeat=repeat)
                    with _LOCK:
                        data["results"].append(result)
                        data["done"] += 1
                        save(data)
                    # 环境失败可能影响之后的所有题，停止而不是继续烧额度。
                    if result["status"] in ("environment_error", "runner_error"):
                        data["error"] = result.get("reason", "环境异常")
                        return
    except Exception as exc:
        data["error"] = f"执行中断：{type(exc).__name__}"
    finally:
        with _LOCK:
            data["status"] = "failed" if data["error"] else "completed" if data["done"] == data["total"] else "cancelled"
            data["phase"] = "已结束"
            data["finished_at"] = time.time()
            try:
                save(data)
            except OSError:
                data.update(status="failed", error="保存结果失败")
        cfg.clear()


def cancel(run_id: str) -> dict | None:
    """停止当前 worker 和后续调度；在途请求的费用不能撤回。

    @param run_id 批次 ID
    @return 当前快照
    """
    run_path(run_id)
    with _LOCK:
        active = _RUNS.get(run_id)
        if active and active["data"]["status"] == "running":
            active["stop"].set()
            active["data"]["status"] = "stopping"
            save(active["data"])
    return get(run_id)
