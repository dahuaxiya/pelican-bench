"""复核前后榜单使用合成快照，禁止读取用户历史或发起模型请求。"""
import copy
import unittest
from unittest.mock import patch

import iq


def run_snapshot(run_id, before, after, completed=100, model="model-a", **changes):
    """生成同一批次的两个评分结果；after=None 模拟未完成复核。

    Args:
        run_id: 合成来源标识。
        before: 原始判分，不受复核修改。
        after: 最终分，None 不得成为正式排名。
        completed: 当前模型完成时间。
        model: 合成模型标识。
        changes: 可覆盖批次字段以验证版本和状态边界。
    Returns:
        与历史查询摘要结构一致的独立字典。
    """
    summary = {"site": "site-a", "model": model, "complete": True,
               "rank_score": before, "completed_at": completed, "thinking_mode": "default",
               "correct": 2, "categories": {}, "normalized": {"rank_score": before},
               "final": {"rank_score": after, "ready": after is not None, "categories": {}}}
    return {"run_id": run_id, "started_at": completed - 10, "finished_at": completed,
            "status": "completed", "suite": {"benchmark": "bbeh-mini", "version": "v1", "formal": True},
            "options": {"repeats": 1}, "normalization_version": "rules-v1", "review_version": "review-v2",
            "summary": [summary], **changes}


class ComparisonTests(unittest.TestCase):
    """双列不能混用来源，也不能改变原评分或让未复核覆盖正式分。"""

    def board(self, runs, **kwargs):
        """只替换历史读取，其余榜单选择代码使用真实实现。"""
        with patch.object(iq, "history", return_value=runs):
            return iq.leaderboard(scoring="comparison", **kwargs)

    def test_pair_uses_latest_reviewed_run_not_best_scores(self):
        """新成绩更低也应替换旧成绩，两列必须来自同一个 run。"""
        old = run_snapshot("old", 90, 95)
        new = run_snapshot("new", 60, 70, 200)
        runs = [old, new]
        saved = copy.deepcopy(runs)
        row = self.board(runs)["entries"][0]
        self.assertEqual((row["run_id"], row["before_review"]["rank_score"], row["after_review"]["rank_score"]),
                         ("new", 60, 70))
        row["before_review"]["categories"]["extra"] = 1
        self.assertEqual(runs, saved)

    def test_pending_review_preserves_previous_final_in_any_history_order(self):
        """历史扫描顺序不应影响旧有效成绩的保留，较新的待复核不能覆盖。"""
        old = run_snapshot("old", 40, 55)
        pending = run_snapshot("pending", 90, None, 200)
        for runs in ([old, pending], [pending, old]):
            with self.subTest(order=[r["run_id"] for r in runs]):
                self.assertEqual(self.board(runs)["entries"][0]["run_id"], "old")

    def test_no_final_shows_latest_before_without_inventing_final_score(self):
        """没有复核完成记录时，原始零分有效，最终分仍为空。"""
        row = self.board([run_snapshot("old", 20, None), run_snapshot("new", 0, None, 200)])["entries"][0]
        self.assertEqual(row["run_id"], "new")
        self.assertEqual(row["before_review"]["rank_score"], 0)
        self.assertIsNone(row["after_review"]["rank_score"])
        self.assertFalse(row["after_review"]["ready"])

    def test_failed_and_smoke_runs_never_create_rank_rows(self):
        """接口缺测或试跑不能占榜单行，也不能形成可选空版本。"""
        invalid = run_snapshot("invalid", None, None)
        incomplete = run_snapshot("incomplete", 90, 90)
        incomplete["summary"][0]["complete"] = False
        smoke = run_snapshot("smoke", 100, 100, suite={"benchmark": "bbeh-mini", "formal": False})
        board = self.board([invalid, incomplete, smoke])
        self.assertEqual(board["entries"], [])
        self.assertEqual(board["cohorts"], [])

    def test_question_and_grading_versions_stay_separate(self):
        """简化界面不能把不同题库或判分协议混入同一排行榜。"""
        base = run_snapshot("base", 40, 50)
        rules = run_snapshot("rules", 50, 60, 200, review_version="review-v3")
        dataset = run_snapshot("data", 60, 70, 300,
                               suite={"benchmark": "bbeh-mini", "version": "v2", "formal": True})
        runs = [base, rules, dataset]
        board = self.board(runs)
        self.assertEqual(len(board["cohorts"]), 3)
        for group in board["cohorts"]:
            self.assertEqual(len(self.board(runs, cohort_id=group["id"])["entries"]), 1)

    def test_old_api_scoring_modes_are_unchanged(self):
        """原有导出和 CLI 的单口径 API 继续返回各自原值。"""
        run = run_snapshot("source", 40, 70)
        run["summary"][0]["normalized"]["rank_score"] = 60
        with patch.object(iq, "history", return_value=[run]):
            for mode, score in (("official", 40), ("normalized", 60), ("reviewed", 70)):
                self.assertEqual(iq.leaderboard(scoring=mode)["entries"][0]["rank_score"], score)
