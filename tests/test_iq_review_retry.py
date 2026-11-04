"""整批补复核使用临时历史和模拟模型，不读取凭据或调用付费渠道。"""
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

import app
import iq
import iq_review
import iq_scoring
import test_iq_history_cache as history_fixture


class ReviewRetryTests(unittest.TestCase):
    """复用隔离存储夹具，额外隔离自动队列，验证原地重试与幂等。"""

    def setUp(self):
        """两项失败、两项已完成和未复核等记录共同构成一批。"""
        history_fixture.HistoryCacheTests.setUp(self)
        for name, value in (("_ACTIVE", set()), ("_AUTO_QUEUE", []), ("_AUTO_THREAD", None)):
            manager = patch.object(iq_review, name, value)
            manager.start()
            self.addCleanup(manager.stop)
        self.cfg = {"sites": [{"name": "judge", "base_url": "https://judge.invalid/v1", "api_key": "test-only"}]}
        self.request = {"run_id": self.data["run_id"], "request_id": "f" * 32,
                        "judge_site": "judge", "judge_model": "judge-model"}
        template = self.data["results"][0]
        self.data["questions"] = []
        self.data["results"] = []
        for i in range(8):
            self.data["questions"].append({"id": f"q{i}", "category": "math", "expected": 1, "prompt": "One?"})
            self.data["results"].append({**template, "question_id": f"q{i}"})
        self.data.update(done=8, total=8)
        self.data["results"][4].update(raw="1", score=1, status="correct")
        self.data["results"][6].update(response_ok=False, raw="", status="service_error")
        iq._persist(self.data)
        for index, status in ((0, "failed"), (1, "running"), (2, "completed"), (3, "completed"), (7, "failed")):
            self.save_item(index, status, score=int(index == 3), stale=index == 7)
        self.original = Path(iq._path(self.data["run_id"])).read_bytes()
        self.response = {"completed": True, "text": json.dumps({"answer": "2", "evidence": "2",
                          "ambiguous": False, "correct": False, "reason": "2 differs from 1"})}
        model = patch.object(iq_review.llm, "stream_chat", return_value=self.response)
        self.call_model = model.start()
        self.addCleanup(model.stop)
        self.addCleanup(self.wait_batch)

    def save_item(self, index, status, score=0, stale=False):
        """保存对应原回答的复核，磁盘 running 表示上次服务中断。

        Args:
            index: 合成题目编号。
            status: 复核状态。
            score: 完成复核的判分，判错也算复核成功。
            stale: 是否模拟原回答已变化的旧复核。
        Returns:
            可用于比较留证是否改变的复核记录。
        """
        row = self.data["results"][index]
        job = {"id": f"{index + 1:032x}", "run_id": self.data["run_id"],
               **{key: row[key] for key in ("site", "model", "question_id", "repeat")},
               "version": iq_review.VERSION, "normalization_version": iq_scoring.VERSION,
               "advisory": False, "status": status, "started_at": 103, "finished_at": 104,
               "source_sha256": "stale" if stale else iq_review.source_hash(self.data["questions"][index], row["raw"]),
               "suggestion": {"status": "correct" if score else "incorrect", "score": score}}
        iq_review._save(job)
        return job

    def wait_batch(self):
        """补复核线程退出后才清理临时目录，测试不得遗留在途请求。"""
        thread = iq_review._AUTO_THREAD
        if thread:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "review dispatcher did not exit")

    def test_retries_failed_and_interrupted_only_without_changing_run(self):
        """只补两项；保留答对、已复核判错、缺测、未复核和原文变化的项。"""
        old_jobs = {path: path.read_bytes() for path in iq_review.ROOT.glob("*.json")}
        batch = iq_review.retry_failed(self.cfg, self.request)
        self.wait_batch()
        final = iq_review.automatic(self.data["run_id"])
        self.assertEqual(batch["total"], 2)
        self.assertEqual((final["status"], final["done"], final["completed"], final["failed"]), ("completed", 2, 2, 0))
        self.assertEqual(self.call_model.call_count, 2)
        self.assertEqual(Path(iq._path(self.data["run_id"])).read_bytes(), self.original)
        self.assertEqual(len(list(Path(iq.ROOT).glob("*.json"))), 1)
        for path, content in old_jobs.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertNotIn("test-only", json.dumps(final))
        self.assertEqual(final["trigger"], "retry_failed")

    def test_concurrent_duplicate_and_restart_are_idempotent(self):
        """同一请求并发提交及服务重启后重发均不重复收费。"""
        with ThreadPoolExecutor(max_workers=6) as pool:
            batches = list(pool.map(lambda _: iq_review.retry_failed(self.cfg, self.request), range(6)))
        self.wait_batch()
        self.assertEqual(len({b["id"] for b in batches}), 1)
        self.assertEqual(self.call_model.call_count, 2)
        iq_review._BATCHES.clear()
        iq_review._JOBS.clear()
        result = iq_review.retry_failed({"sites": []}, self.request)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.call_model.call_count, 2)
        with self.assertRaisesRegex(ValueError, "不能修改参数"):
            iq_review.retry_failed(self.cfg, {**self.request, "judge_model": "different"})

    def test_active_batch_rejects_different_request_and_reports_progress(self):
        """未结束的一轮拒绝另开一轮；原 ID 重发则返回在途状态。"""
        entered = threading.Event()
        release = threading.Event()
        def model(*args, **kwargs):
            """等待测试放行，以便读取在途状态和模拟双击。"""
            entered.set()
            release.wait(5)
            return self.response
        self.call_model.side_effect = model
        try:
            iq_review.retry_failed(self.cfg, self.request)
            self.assertTrue(entered.wait(3))
            batch = iq_review.retry_failed(self.cfg, self.request)
            self.assertEqual(batch["status"], "running")
            with self.assertRaisesRegex(ValueError, "正在复核"):
                iq_review.retry_failed(self.cfg, {**self.request, "request_id": "e" * 32})
        finally:
            release.set()
            self.wait_batch()

    def test_another_explicit_round_can_retry_remaining_failure(self):
        """复核仍失败可再开一轮，已完成项不再请求。"""
        self.call_model.return_value = {"completed": False}
        first = iq_review.retry_failed(self.cfg, self.request)
        self.wait_batch()
        self.assertEqual(iq_review.automatic(self.data["run_id"])["failed"], 2)
        self.call_model.return_value = self.response
        second = iq_review.retry_failed(self.cfg, {**self.request, "request_id": "e" * 32})
        self.wait_batch()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.call_model.call_count, 4)
        with self.assertRaisesRegex(ValueError, "没有失败"):
            iq_review.retry_failed(self.cfg, {**self.request, "request_id": "d" * 32})

    def test_restart_does_not_auto_resend_queued_batch(self):
        """持久化成功但进程退出的队列返回中断；同 ID 不自动补发。"""
        with patch.object(iq_review, "_dispatch_auto"):
            batch = iq_review.retry_failed(self.cfg, self.request)
            self.wait_batch()
        iq_review._BATCHES.clear()
        iq_review._AUTO_QUEUE.clear()
        result = iq_review.retry_failed(self.cfg, self.request)
        self.assertEqual(result["id"], batch["id"])
        self.assertEqual(result["status"], "interrupted")
        self.call_model.assert_not_called()

    def test_queue_reuses_individual_review_completed_in_the_meantime(self):
        """排队期间逐题复核判错也属已完成，批量工作不得重复请求。"""
        with patch.object(iq_review, "_dispatch_auto"):
            batch = iq_review.retry_failed(self.cfg, self.request)
            self.wait_batch()
        self.save_item(0, "completed", score=0)
        iq_review._dispatch_auto()
        self.assertEqual(self.call_model.call_count, 1)
        final = iq_review.automatic(self.data["run_id"])
        self.assertEqual((final["completed"], final["reused"]), (2, 1))

    def test_rejects_unready_judge_and_untrusted_fields(self):
        """只接受固定字段、合法 ID 和已配置站点，不允许请求传凭据或 URL。"""
        for changes in ({"run_id": "../bad"}, {"judge_site": "missing"}, {"judge_model": ""},
                        {"base_url": "https://evil.invalid"}, {"api_key": "secret"}):
            with self.assertRaises(ValueError):
                iq_review.retry_failed(self.cfg, {**self.request, **changes})
        cfg = copy.deepcopy(self.cfg)
        cfg["sites"][0]["ready"] = False
        with self.assertRaises(ValueError):
            iq_review.retry_failed(cfg, self.request)
        self.call_model.assert_not_called()


class ReviewRetryRouteTests(unittest.TestCase):
    """不监听端口，直接验证路由沿用同源和节点切换保护。"""

    def handler(self, origin="http://127.0.0.1:8780", content_type="application/json"):
        """建立不含 socket 的请求对象。

        Args:
            origin: 浏览器来源。
            content_type: 请求格式。
        Returns:
            仅记录响应的 Handler。
        """
        handler = app.Handler.__new__(app.Handler)
        handler.headers = Message()
        handler.headers["Origin"] = origin
        handler.headers["Host"] = "127.0.0.1:8780"
        handler.headers["Content-Type"] = content_type
        handler._read_json = Mock(return_value={"run_id": "a" * 32})
        handler._json = Mock()
        return handler

    def test_origin_and_content_type_block_before_scheduling(self):
        """跨源与表单请求在创建复核前拒绝。"""
        with patch.object(iq_review, "retry_failed") as retry:
            for origin, content_type, code in (("https://foreign.invalid", "application/json", 403),
                                               ("http://127.0.0.1:8780", "text/plain", 415)):
                handler = self.handler(origin, content_type)
                handler._api_iq_post("/api/iq/review-retry")
                self.assertEqual(handler._json.call_args.args[1], code)
            retry.assert_not_called()

    def test_route_and_network_exclusion(self):
        """合法请求转交整批入口；节点切换期间拒绝创建任务。"""
        handler = self.handler()
        with patch.object(app.config, "load", return_value={}), patch.object(iq_review, "retry_failed", return_value={"id": "x"}) as retry:
            handler._api_iq_post("/api/iq/review-retry")
            retry.assert_called_once_with({}, {"run_id": "a" * 32})
        handler._host_ok = Mock(return_value=True)
        handler._instance_ok = Mock(return_value=True)
        handler.server = Mock(stopping=False)
        handler.path = "/api/iq/review-retry"
        with patch.object(app.network_probe.manager, "busy", return_value=True), patch.object(iq_review, "retry_failed") as retry:
            handler._dispatch_post()
            self.assertEqual(handler._json.call_args.args[1], 409)
            retry.assert_not_called()
