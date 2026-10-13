"""覆盖 CLI 的多类任务路由、复核等待和失败退出码，不调用真实模型。"""
import contextlib
import io
import unittest
from unittest.mock import patch

import cli_commands as cli


class FakeClient:
    """预置响应并记录 API 参数，以验证 CLI 没有另起判分逻辑。"""

    def __init__(self, responses):
        """每个响应消费一次，多余请求视为测试失败。"""
        self.responses = iter(responses)
        self.calls = []

    def request(self, path, body=None):
        """返回下一个预设快照，记录实际路径和提交体。"""
        self.calls.append((path, body))
        return next(self.responses)


class CliTests(unittest.TestCase):
    """检查网页与 CLI 共享协议的边界，以及机器可读输出。"""

    def test_benchmark_routes_and_budgets(self):
        """五类测评都提交正确 API；IQ 请求超时字段转换，不覆盖用户预算。"""
        routes = {"performance": "/api/probe", "capacity": "/api/probe", "svg": "/api/run",
                  "iq": "/api/iq/run", "engineering": "/api/swe/run"}
        for kind, route in routes.items():
            with self.subTest(kind=kind), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                argv = ["test", kind, "--target", "site one", "model/x", "--no-wait"]
                if kind in ("iq", "engineering"):
                    argv += ["--request-timeout", "120"]
                args = cli.parser().parse_args(argv)
                client = FakeClient([{"run_id": "a" * 32}])
                self.assertEqual(cli.execute(args, client), 0)
                path, body = client.calls[0]
                self.assertEqual(path, route)
                self.assertEqual(body.get("targets", body.get("tasks")), [{"site": "site one", "model": "model/x"}])
                if kind in ("iq", "engineering"):
                    self.assertEqual(body["timeout_seconds" if kind == "iq" else "request_timeout"], 120)
                    self.assertEqual(len(body["request_id"]), 32)

    def test_wait_includes_retry_and_review(self):
        """回答完成后仍等待复核；只有 ready 最终分返回 0，进度不能污染 JSON 通道。"""
        snapshots = [
            {"status": "running", "done": 9, "total": 10, "auto_retry": {"round": 1}},
            {"status": "completed", "summary": [{"final": {"pending": 1, "ready": False}}]},
            {"status": "completed", "auto_review": {"status": "running", "done": 0, "total": 1}},
            {"status": "completed", "auto_review": {"status": "completed"}, "summary": [{"final": {"pending": 0, "ready": True, "ability_score": 90}}]},
        ]
        client = FakeClient(snapshots)
        out, err = io.StringIO(), io.StringIO()
        with patch("cli_commands.time.sleep"), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result, code = cli.wait_result(client, "iq", "a" * 32)
        self.assertEqual(code, 0)
        self.assertTrue(result["summary"][0]["final"]["ready"])
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("复核 running", err.getvalue())
        unavailable = {"status": "completed", "auto_review": {"status": "unavailable"},
                       "summary": [{"final": {"pending": 1, "ready": False}}]}
        self.assertFalse(cli.pending("iq", unavailable))
        self.assertEqual(cli.result_exit("iq", unavailable), 3)

    def test_failures_are_not_successful_commands(self):
        """答错或未修复是有效测量；接口失败、SVG 无效和取消不能返回成功码。"""
        self.assertEqual(cli.result_exit("performance", {"status": "completed", "rows": [{"status": "failed"}]}), 3)
        self.assertEqual(cli.result_exit("svg", {"done": 1, "total": 1, "results": [{"ok": False}]}), 3)
        self.assertEqual(cli.result_exit("engineering", {"status": "completed", "summary": [{"valid": False}]}), 3)
        self.assertEqual(cli.result_exit("engineering", {"status": "completed", "summary": [{"valid": True, "success_rate": 0}]}), 0)

    def test_target_selection_and_id_escaping(self):
        """重复组合去重，缺省模型不扩大测评范围，ID 不能变成路径或查询参数。"""
        args = cli.parser().parse_args(["test", "iq", "--site", "x", "--site", "x", "--model", "a"])
        self.assertEqual(cli.select_targets(args, FakeClient([])), [{"site": "x", "model": "a"}])
        args = cli.parser().parse_args(["test", "iq", "--site", "x"])
        with self.assertRaises(ValueError):
            cli.select_targets(args, FakeClient([]))
        self.assertEqual(cli.result_path("iq", "../config?x=1"), "/api/iq/run/..%2Fconfig%3Fx%3D1")


if __name__ == "__main__":
    unittest.main()
