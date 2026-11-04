"""回放 exec JSONL 协议，不使用账号或真实模型请求。"""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_channel as channel
import llm


SERVER = r'''
import json, sys
scenario = sys.argv[1]
assert sys.stdin.read() == "test"
def emit(kind, **kwargs):
    print(json.dumps({"type": kind, **kwargs}), flush=True)
if scenario == "startup":
    emit("item.completed", item={"type": "error", "message": "Code Mode is unavailable because code-mode host is disabled."})
emit("turn.started")
if scenario in ("recover", "failed", "empty"):
    emit("item.completed", item={"type": "error", "message": "HTTP 503 Selected model is at capacity. sk-private-value"})
if scenario == "failed":
    emit("turn.failed", error={"message": "HTTP 503 Selected model is at capacity. sk-private-value"})
elif scenario == "tool":
    emit("item.started", item={"type": "command_execution", "command": "sk-private-command"})
elif scenario == "unknown":
    emit("item.completed", item={"type": "sk-private-value", "text": "private body"})
elif scenario == "malformed":
    emit("item.completed", item={"type": []})
elif scenario == "incomplete":
    emit("item.completed", item={"type": "reasoning", "text": "private"})
else:
    if scenario != "empty":
        emit("item.completed", item={"type": "agent_message", "text": "OK"})
    emit("turn.completed", usage={"input_tokens": 5, "output_tokens": 1})
'''


class ExecEventTests(unittest.TestCase):
    """诊断可以恢复，工具和未知事件不能被放行或泄漏正文。"""

    def run_sample(self, scenario):
        """用独立本地子进程回放场景，并确认每条退出路径回收进程。

        Args:
            scenario: 测试内的固定场景名。
        Returns:
            正式 exec 适配器返回值；失败继续抛出以验证错误分类。
        """
        with patch.object(channel, "binary_path", return_value=sys.executable), \
                patch.object(channel, "_environment", return_value=os.environ.copy()), \
                patch.object(channel, "_command", return_value=[sys.executable, "-u", "-c", SERVER, scenario]), \
                patch.object(channel, "_kill", wraps=channel._kill) as cleanup:
            try:
                return channel.complete("gpt-test", "test", 3)
            finally:
                self.assertEqual(cleanup.call_count, 1)
                self.assertIsNotNone(cleanup.call_args.args[0].poll())

    def test_diagnostic_item_can_recover(self):
        """错误通知之后的完整成功回答可接受，同时保留容量证据。"""
        result = self.run_sample("recover")
        self.assertEqual(result["text"], "OK")
        self.assertTrue(result["transport_diagnostics"]["capacity_detected"])
        self.assertEqual(result["transport_diagnostics"]["error_events"], 1)
        self.assertIsNone(result["ttft"])
        self.assertNotIn("sk-private", json.dumps(result))

    def test_startup_notice_does_not_fail_or_count_as_upstream_error(self):
        """本地禁用 code mode 的已知提示不是上游故障，不改变原有隔离策略。"""
        result = self.run_sample("startup")
        self.assertEqual(result["text"], "OK")
        self.assertEqual(result["transport_diagnostics"]["error_events"], 0)

    def test_terminal_failure_is_classified_without_raw_error(self):
        """真正的失败终态保留容量分类及通知次数，不能误标工具事件。"""
        with self.assertRaises(channel.CodexRequestError) as raised:
            self.run_sample("failed")
        self.assertIn("[capacity]", str(raised.exception))
        self.assertNotIn("[tool]", str(raised.exception))
        self.assertNotIn("sk-private", str(raised.exception))
        self.assertTrue(raised.exception.transport_diagnostics["capacity_detected"])

    def test_tool_unknown_and_malformed_items_fail_distinctly(self):
        """明确工具、未知协议和畸形 item 分别失败；任何原始参数都不进入异常。"""
        for scenario, expected in (("tool", "[tool]"), ("unknown", "[protocol]"), ("malformed", "格式异常")):
            with self.subTest(scenario=scenario), self.assertRaises(channel.CodexRequestError) as raised:
                self.run_sample(scenario)
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn("sk-private", str(raised.exception))
            self.assertNotIn("private body", str(raised.exception))

    def test_empty_and_incomplete_answers_do_not_become_successes(self):
        """诊断后没有答案仍报空正文，缺失完成事件则直接失败。"""
        result = self.run_sample("empty")
        self.assertTrue(result["error"])
        self.assertEqual(result["text"], "")
        with self.assertRaises(llm.LLMError):
            self.run_sample("incomplete")


if __name__ == "__main__":
    unittest.main()
