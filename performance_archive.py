"""性能测评按批次保存；对比视图只使用已结束批次的有效长输出成绩。"""
import copy
import json
import math
from pathlib import Path
import re
import threading
import time


class ProbeArchive:
    """独立于当前任务的历史存储，沿用项目的原子 JSON 文件保存方式。"""

    def __init__(self, directory=None):
        """只读加载归档；由实际启动的服务单独处理重启中断，导入模块不会改写记录。

        Args:
            directory: 服务端固定目录；None 只保存在内存，供隔离测试使用。
        """
        self.directory = Path(directory) if directory else None
        self.lock = threading.RLock()
        self.runs = {}
        self.error = None
        if self.directory and self.directory.exists():
            for path in self.directory.glob("*.json"):
                try:
                    run = self._normalize(json.loads(path.read_text(encoding="utf-8")))
                    self.runs[run["id"]] = run
                except (OSError, ValueError, TypeError):
                    # 一份损坏的文件不能隐藏其余历史，但也不能悄悄宣称全部加载成功。
                    self.error = "部分历史记录无法读取，原文件已保留"

    def interrupt_incomplete(self):
        """仅在服务启动且端口绑定成功后调用，将遗留在途记录标为中断而不重发请求。"""
        with self.lock:
            for snapshot in list(self.runs.values()):
                if snapshot["status"] not in ("running", "stopping"):
                    continue
                run = copy.deepcopy(snapshot)
                run.update(status="cancelled", finished_at=time.time())
                for row in run["rows"]:
                    if row.get("status") in ("running", "queued"):
                        row.update(status="cancelled", reason="服务重启，检测已中断")
                self.record(run)

    @staticmethod
    def _normalize(snapshot):
        """校验导入结构并只保留测试元数据，正文和连接配置不能进入历史。

        Args:
            snapshot: 当前任务或旧版导出的 JSON 对象。
        Returns:
            独立副本；缺少合法批次 ID、状态或模型行时抛出 ValueError。
        """
        if (not isinstance(snapshot, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(snapshot.get("id", "")))
                or snapshot.get("status") not in ("running", "stopping", "completed", "cancelled")
                or not isinstance(snapshot.get("rows"), list)
                or type(snapshot.get("started_at")) not in (int, float)
                or not math.isfinite(snapshot["started_at"])):
            raise ValueError("无效的性能测试记录")
        run = {key: copy.deepcopy(snapshot[key]) for key in (
            "id", "status", "mode", "site", "sites", "started_at", "finished_at", "concurrency", "timeout_seconds"
        ) if key in snapshot}
        run["mode"] = run.get("mode") or "capacity"
        run["rows"] = []
        for row in snapshot["rows"]:
            if (not isinstance(row, dict) or not isinstance(row.get("model"), str)
                    or not isinstance(row.get("site") or run.get("site") or "未记录站点", str)
                    or (row.get("metrics") is not None and not isinstance(row["metrics"], dict))):
                raise ValueError("无效的模型测试记录")
            item = {key: copy.deepcopy(row[key]) for key in (
                "site", "model", "status", "events", "capacity_detected", "metrics", "transport",
                "reason", "response_model", "started_at", "finished_at", "duration_seconds", "recovery_seconds"
            ) if key in row}
            item["site"] = item.get("site") or run.get("site") or "未记录站点"
            run["rows"].append(item)
        return run

    def record(self, snapshot):
        """更新同一批次，不覆盖其他批次；写入失败保留内存结果并显式提示。

        Args:
            snapshot: 不含正文或连接凭据的当前测试状态。
        """
        run = self._normalize(snapshot)
        with self.lock:
            self.runs[run["id"]] = run
            if not self.directory:
                return
            # 所有写操作共用锁和临时文件替换，读者不会遇到写了一半的 JSON。
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                target = self.directory / (run["id"] + ".json")
                temporary = target.with_suffix(".tmp")
                temporary.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
                temporary.replace(target)
            except OSError:
                self.error = "历史记录保存失败，重启后可能丢失；请检查本地磁盘"

    def import_snapshots(self, paths):
        """幂等导入旧版明确保存过的快照，不覆盖已归档批次或导入未结束快照。

        Args:
            paths: 服务端选定的本地文件列表，不接受客户端路径。
        """
        for path in paths:
            try:
                run = self._normalize(json.loads(Path(path).read_text(encoding="utf-8")))
                with self.lock:
                    if run["id"] not in self.runs and run["status"] in ("completed", "cancelled"):
                        self.record(run)
            except (OSError, ValueError, TypeError):
                continue

    def get(self, run_id):
        """按不透明批次 ID 返回副本，不把 URL 参数拼成文件路径。

        Args:
            run_id: 完整的 32 位十六进制测试 ID。
        Returns:
            记录副本或 None；非法 ID 抛出 ValueError。
        """
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("无效的测试 ID")
        with self.lock:
            return copy.deepcopy(self.runs.get(run_id))

    def listing(self, view="compare", site="", model="", concurrency="", sort="speed", page=1, page_size=20):
        """筛选、去重、排序后再分页；较新的失败或未结束批次不能替换有效成绩。

        Args:
            view: compare 为最新有效测速成绩，history 为完整批次历史。
            site: 精确站点名；空字符串包含全部。
            model: 精确模型名；空字符串包含全部。
            concurrency: 精确并发上限，空字符串不限；同一模型可据此比较不同负载。
            sort: speed、latency 或 recent；未知数值始终排最后。
            page: 从 1 开始的页码，超过末页时回到末页。
            page_size: 每页 1 到 100 项。
        Returns:
            当前页、筛选选项、总数和持久化错误，不返回模型正文。
        """
        if view not in ("compare", "history") or sort not in ("speed", "latency", "recent"):
            raise ValueError("无效的历史视图或排序")
        page, page_size = int(page), int(page_size)
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("无效的页码或每页数量")
        if concurrency and not 1 <= int(concurrency) <= 200:
            raise ValueError("无效的并发筛选")
        with self.lock:
            runs = sorted(self.runs.values(), key=lambda run: (run["started_at"], run["id"]), reverse=True)
            facets = {
                "sites": sorted({row["site"] for run in runs for row in run["rows"]}),
                "models": sorted({row["model"] for run in runs for row in run["rows"]}),
                "concurrencies": sorted({run["concurrency"] for run in runs if run.get("concurrency")}),
            }
            entries, latest, counts = [], {}, {}
            for run in runs:
                if concurrency and str(run.get("concurrency", "")) != str(concurrency):
                    continue
                rows = [row for row in run["rows"] if (not site or row["site"] == site)
                        and (not model or row["model"] == model)]
                if not rows:
                    continue
                if view == "history":
                    entries.append({key: value for key, value in run.items() if key != "rows"} | {
                        "sites": sorted({row["site"] for row in run["rows"]}),
                        "total": len(run["rows"]), "success": sum(row.get("status") == "success" for row in run["rows"]),
                        "failed": sum(row.get("status") == "failed" for row in run["rows"]),
                        "models": [row["model"] for row in run["rows"]],
                    })
                    continue
                if run["mode"] != "performance" or run["status"] not in ("completed", "cancelled"):
                    continue
                for row in rows:
                    key = (row["site"], row["model"])
                    counts[key] = counts.get(key, 0) + 1
                    latest.setdefault(key, row.get("status"))
                    metrics = row.get("metrics") or {}
                    speed = metrics.get("end_to_end_tokens_per_second")
                    # 成功但缺少真实用量、输出过短等情况，不是可比较的有效测速成绩。
                    if (row.get("status") == "success" and type(speed) in (int, float)
                            and math.isfinite(speed) and speed > 0 and not metrics.get("sample_short")):
                        entries.append({**row, "run_id": run["id"], "tested_at": run["started_at"],
                                        "concurrency": run.get("concurrency"), "timeout_seconds": run.get("timeout_seconds")})
            if view == "compare":
                unique = {}
                for entry in entries:
                    key = (entry["site"], entry["model"])
                    unique.setdefault(key, {**entry, "attempts": counts[key], "latest_status": latest[key]})
                entries = list(unique.values())
                if sort == "speed":
                    entries.sort(key=lambda item: -item["metrics"]["end_to_end_tokens_per_second"])
                elif sort == "latency":
                    entries.sort(key=lambda item: item["metrics"].get("ttft_seconds")
                                 if item["metrics"].get("ttft_seconds") is not None else math.inf)
                else:
                    entries.sort(key=lambda item: -item["tested_at"])
            total = len(entries)
            page = min(page, max(1, math.ceil(total / page_size)))
            return copy.deepcopy({"items": entries[(page - 1) * page_size:page * page_size],
                "total": total, "page": page, "page_size": page_size, "facets": facets,
                "max_speed": max((item["metrics"]["end_to_end_tokens_per_second"] for item in entries), default=0)
                    if view == "compare" else 0,
                "active": any(run["status"] in ("running", "stopping") for run in runs), "storage_error": self.error})
