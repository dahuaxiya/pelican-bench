"""用隔离历史验证榜单缓存的新鲜度，不读取用户数据或请求模型。"""
import copy
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import iq
import iq_review
import iq_scoring


def sample_run(run_id="a" * 32):
    """生成一题答错的完整批次，用于验证复核前后的分数变化。

    Args:
        run_id: 临时目录中的合法批次 ID。
    Returns:
        只含合成题目与回答的原始评测记录。
    """
    return {"run_id": run_id, "status": "completed", "started_at": 100, "finished_at": 102,
            "done": 1, "total": 1, "options": {"repeats": 1},
            "suite": {"id": "fixture", "version": "fixture-v1", "formal": True, "weights": {"math": 1}},
            "questions": [{"id": "q1", "category": "math", "expected": 1, "prompt": "One?"}],
            "configurations": [{"site": "site", "model": "model", "thinking_mode": "default"}],
            "results": [{"site": "site", "model": "model", "question_id": "q1", "repeat": 1,
                         "category": "math", "status": "incorrect", "response_ok": True,
                         "raw": "2", "score": 0, "started_at": 100, "duration_seconds": 2,
                         "ttft": 1, "cost_estimate_usd": None}]}


class HistoryCacheTests(unittest.TestCase):
    """命中缓存不能隐藏重试、复核、版本变化或后续删除的记录。"""

    def setUp(self):
        """把运行、复核和摘要缓存全部隔离，避免测试改写本机成绩。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for module, name, value in (
            (iq, "ROOT", str(self.root / "runs")), (iq, "_RUNS", {}),
            (iq, "_SUMMARY_CACHE", iq.OrderedDict()),
            (iq_review, "ROOT", self.root / "reviews"),
            (iq_review, "_JOBS", {}), (iq_review, "_BATCHES", {}),
        ):
            manager = patch.object(module, name, value)
            manager.start()
            self.addCleanup(manager.stop)
        self.data = sample_run()
        iq._persist(self.data)

    def save_review(self, status="completed", score=1):
        """保存绑定当前答案的复核证据，模拟后台作业完成或失败。

        Args:
            status: 复核任务状态。
            score: 完成时的合成判分。
        Returns:
            已持久化的复核字典，可用于模拟内存中的在途任务。
        """
        job = {"id": "b" * 32, "run_id": self.data["run_id"],
               "site": "site", "model": "model", "question_id": "q1", "repeat": 1,
               "version": iq_review.VERSION, "normalization_version": iq_scoring.VERSION,
               "advisory": False, "status": status, "started_at": 103, "finished_at": 104,
               "source_sha256": iq_review.source_hash(self.data["questions"][0], self.data["results"][0]["raw"]),
               "suggestion": {"status": "correct" if score else "incorrect", "score": score}}
        with iq_review._LOCK:
            iq_review._save(job)
        return job

    def test_cached_summary_matches_full_result_and_is_independent(self):
        """摘要命中不重算，完整回答仍可按需读取，调用方修改不会污染缓存。"""
        with patch.object(iq, "summaries", wraps=iq.summaries) as calculate:
            first = iq.get(self.data["run_id"])
            second = iq.get(self.data["run_id"])
            self.assertEqual(first, second)
            self.assertEqual(calculate.call_count, 1)
            second["summary"][0]["rank_score"] = 999
            self.assertEqual(iq.get(self.data["run_id"]), first)
            full = iq.get(self.data["run_id"], full=True)
            self.assertEqual(full["summary"], first["summary"])
            self.assertIn("results", full)
            self.assertNotIn("results", first)

    def test_review_completion_failure_and_deletion_invalidate_score(self):
        """原答案不变，复核增改删也必须改变最终分，不发布旧缓存。"""
        self.assertFalse(iq.get(self.data["run_id"])["summary"][0]["final"]["ready"])
        self.save_review()
        self.assertEqual(iq.leaderboard()["entries"][0]["rank_score"], 100)
        self.save_review(score=0)
        self.assertEqual(iq.leaderboard()["entries"][0]["rank_score"], 0)
        self.save_review(status="failed")
        self.assertEqual(iq.leaderboard()["entries"], [])
        iq_review._path("b" * 32).unlink()
        self.assertEqual(iq.get(self.data["run_id"])["summary"][0]["final"]["pending"], 1)

    def test_in_place_answer_change_invalidates_old_review(self):
        """原地重试替换了回答时，旧复核的原文哈希不能继续生效。"""
        self.save_review()
        self.assertEqual(iq.leaderboard()["entries"][0]["rank_score"], 100)
        self.data["results"][0]["raw"] = "3"
        iq._persist(self.data)
        self.assertEqual(iq.leaderboard()["entries"], [])
        self.data["results"][0].update(raw="1", score=1, status="correct")
        iq._persist(self.data)
        self.assertEqual(iq.leaderboard()["entries"][0]["rank_score"], 100)

    def test_active_memory_and_batch_progress_do_not_use_stale_disk_state(self):
        """在途重试、内存复核和自动复核进度保持实时，重启遗留任务显示中断。"""
        job = self.save_review(status="running")
        self.assertEqual(iq_review.history(self.data["run_id"])[0]["status"], "interrupted")
        iq_review._JOBS[job["id"]] = job
        self.assertEqual(iq_review.history(self.data["run_id"])[0]["status"], "running")
        job["status"] = "completed"
        self.assertTrue(iq.get(self.data["run_id"])["summary"][0]["final"]["ready"])
        batch = {"id": "c" * 32, "run_id": self.data["run_id"], "started_at": 105,
                 "status": "running", "completed": 0, "job_ids": [job["id"]]}
        iq_review._BATCHES[batch["id"]] = batch
        self.assertEqual(iq.get(self.data["run_id"])["auto_review"]["completed"], 0)
        batch.update(status="completed", completed=1)
        self.assertEqual(iq.get(self.data["run_id"])["auto_review"]["completed"], 1)
        run = iq.Run({}, copy.deepcopy(self.data))
        iq._RUNS[self.data["run_id"]] = run
        run.data["status"] = "running"
        self.assertFalse(iq.get(self.data["run_id"])["summary"][0]["final"]["ready"])

    def test_history_scans_reviews_once_and_detects_added_deleted_runs(self):
        """每次历史请求只扫描一遍复核目录；增删批次无需重启服务。"""
        iq._persist(sample_run("d" * 32))
        with patch.object(iq_review, "snapshot_index", wraps=iq_review.snapshot_index) as index:
            self.assertEqual(len(iq.history()), 2)
            self.assertEqual(index.call_count, 1)
        Path(iq._path("d" * 32)).unlink()
        self.assertEqual(len(iq.history()), 1)
        iq._persist(sample_run("e" * 32))
        self.assertEqual(len(iq.history()), 2)

    def test_versions_and_concurrent_reads(self):
        """同时读取同一批只计算一次，评分协议升级则重新派生成绩。"""
        with patch.object(iq, "summaries", wraps=iq.summaries) as calculate:
            with ThreadPoolExecutor(max_workers=8) as pool:
                snapshots = list(pool.map(iq.get, [self.data["run_id"]] * 8))
            self.assertTrue(all(row == snapshots[0] for row in snapshots))
            self.assertEqual(calculate.call_count, 1)
            with patch.object(iq_review, "VERSION", "future-review"):
                self.assertEqual(iq.get(self.data["run_id"])["review_version"], "future-review")
            self.assertEqual(calculate.call_count, 2)

    def test_replaced_review_with_preserved_mtime_and_malformed_neighbors(self):
        """复核备份恢复保留修改时间也会失效；损坏的邻居文件不应拖垮全部榜单。"""
        self.save_review()
        self.assertEqual(iq.leaderboard()["entries"][0]["rank_score"], 100)
        path = iq_review._path("b" * 32)
        previous = path.stat()
        job = self.save_review(score=0)
        # 保持 JSON 字节数和 mtime 与旧文件相同，只靠文件身份识别替换。
        job["suggestion"]["status"] = "correct"
        iq_review._save(job)
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertEqual(path.stat().st_size, previous.st_size)
        iq_review._path("f" * 32).write_text("[]", encoding="utf-8")
        iq_review._path("e" * 32).write_text("{", encoding="utf-8")
        self.assertEqual(iq.leaderboard()["entries"][0]["rank_score"], 0)
