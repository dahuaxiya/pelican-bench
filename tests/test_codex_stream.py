"""用真实子进程回放 app-server 协议，覆盖首字计时、取消及失败边界。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_stream
import codex_probe
import llm


# 替身读取真实 stdin 并按 RPC 握手回包；不连接账号，不读取任何认证文件。
SERVER = r'''
import json, os, sys, time
scenario = sys.argv[1]
if scenario == "proxy":
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert os.environ[key] == "http://127.0.0.1:12345"
    assert os.environ["NO_PROXY"] == os.environ["no_proxy"] == ""
def emit(event):
    print(json.dumps(event), flush=True)
def note(method, **params):
    emit({"method": method, "params": {"threadId": "thread", "turnId": "turn", **params}})
for line in sys.stdin:
    request = json.loads(line)
    method, params = request["method"], request["params"]
    if method == "initialize":
        if scenario == "hang_init":
            time.sleep(10)
        emit({"id": request["id"], "result": {}})
    elif method == "thread/start":
        assert params["ephemeral"] and params["sandbox"] == "read-only"
        assert params["approvalPolicy"] == "never" and params["modelProvider"] == "openai"
        emit({"id": request["id"], "result": {"thread": {"id": "thread"}}})
    elif method == "turn/start":
        assert params["effort"] == "medium" and params["input"][0]["type"] == "text"
        # app-server 允许通知先于 RPC 响应到达。
        note("turn/started", turn={"id": "turn", "status": "inProgress"})
        emit({"id": request["id"], "result": {"turn": {"id": "turn"}}})
        note("item/started", item={"id": "user", "type": "userMessage"})
        note("item/reasoning/textDelta", delta="Thinking")
        note("item/agentMessage/delta", delta="OTHER", threadId="unrelated")
        note("item/agentMessage/delta", delta="OTHER", turnId="unrelated")
        note("item/agentMessage/delta", delta="  ")
        if scenario in ("capacity", "failure"):
            note("error", error={"message": "HTTP 503 Selected model is at capacity. sk-test-secret"})
        if scenario == "tool":
            note("item/started", item={"id": "tool", "type": "commandExecution"})
        if scenario == "approval":
            emit({"id": 99, "method": "item/commandExecution/requestApproval", "params": {}})
        if scenario == "bad_json":
            print("{bad", flush=True)
        time.sleep(.08)
        if scenario != "no_delta":
            note("item/agentMessage/delta", delta="Hello", itemId="answer")
            note("item/agentMessage/delta", delta=" world", itemId="answer")
        if scenario == "hang_turn":
            time.sleep(10)
        time.sleep(.08)
        note("item/completed", item={"id": "answer", "type": "agentMessage", "text": "Hello world"})
        if scenario == "eof":
            break
        if scenario != "no_usage":
            total = {"inputTokens": 20, "outputTokens": 8, "reasoningOutputTokens": 2,
                     "cachedInputTokens": 3, "totalTokens": 28}
            if scenario == "bad_usage":
                total["outputTokens"] = True
            note("thread/tokenUsage/updated", tokenUsage={"total": total, "last": {"outputTokens": 1}})
        if scenario == "reroute":
            note("model/rerouted", fromModel="gpt-test", toModel="gpt-other")
        note("turn/completed", turn={"id": "turn", "status": "failed" if scenario == "failure" else "completed"})
        time.sleep(10)
'''


class StreamTests(unittest.TestCase):
    """所有测试使用可回收的本地替身；错误路径也必须释放进程和管道。"""

    def run_sample(self, scenario="normal", timeout=3, stop=None, seen=None, proxy=None):
        """启动协议替身并调用正式适配器，记录进程以便断言没有遗留子进程。

        Args:
            scenario: 受控故障类型，不能来自用户输入。
            timeout: 测试总时限，单位秒。
            stop: 可选取消事件。
            seen: 可选输出元数据容器。
            proxy: 可选回环代理，用于核对仅本次子进程的路由配置。
        Returns:
            正式适配器结果；异常按正式行为传播。
        """
        with patch.object(codex_stream.codex_auth, "selected_home", return_value=Path("/tmp/unused-auth")), \
                patch.object(codex_stream.channel, "binary_path", return_value=sys.executable), \
                patch.object(codex_stream.channel, "_environment", return_value=os.environ.copy()), \
                patch.object(codex_stream, "_command", return_value=[sys.executable, "-u", "-c", SERVER, scenario]), \
                patch.object(codex_stream.channel, "_kill", wraps=codex_stream.channel._kill) as cleanup:
            try:
                return codex_stream.complete("gpt-test", "test", timeout, stop,
                    on_output=seen.append if seen is not None else None, proxy_url=proxy)
            finally:
                self.assertEqual(cleanup.call_count, 1)
                self.assertIsNotNone(cleanup.call_args.args[0].poll())

    def test_first_body_delta_precedes_completed_message(self):
        """只有本轮非空正文触发首字，且完整消息和用量仍等待完成事件。"""
        seen = []
        result = self.run_sample(seen=seen)
        self.assertGreater(result["ttft"], .07)
        self.assertGreater(result["performance"]["first_message_seconds"] - result["ttft"], .06)
        self.assertGreaterEqual(result["performance"]["response_seconds"], result["performance"]["first_message_seconds"])
        self.assertEqual(result["chunks"], 2)
        self.assertEqual(result["usage"]["completion_tokens"], 8)
        self.assertEqual(result["text"], "Hello world")
        self.assertEqual(seen, [{"kind": "text_delta", "elapsed_seconds": result["ttft"]}])
        self.assertNotIn("Hello", json.dumps(seen))

    def test_proxy_is_subprocess_only_and_requires_website_account(self):
        """节点代理覆盖大小写和绕过配置，但不能改动后端的环境或回退桌面账号。"""
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9000", "NO_PROXY": "*"}):
            self.assertTrue(self.run_sample("proxy", proxy="http://127.0.0.1:12345")["completed"])
            self.assertEqual(os.environ["HTTPS_PROXY"], "http://127.0.0.1:9000")
            self.assertEqual(os.environ["NO_PROXY"], "*")
        with patch.object(codex_stream.codex_auth, "selected_home", return_value=None), \
                patch.object(codex_stream.channel, "complete") as legacy:
            with self.assertRaises(llm.LLMError):
                codex_stream.complete("gpt-test", "test", 1, proxy_url="http://127.0.0.1:12345")
            legacy.assert_not_called()

    def test_completed_message_does_not_fill_missing_delta(self):
        """缺少 delta 时首字必须保持未知，不能用完整消息时间补造。"""
        result = self.run_sample("no_delta")
        self.assertIsNone(result["ttft"])
        self.assertIsNotNone(result["performance"]["first_message_seconds"])

    def test_capacity_survives_recovery_without_leaking_raw_error(self):
        """重连成功仍保留容量证据，但错误凭据不进入返回值。"""
        result = self.run_sample("capacity")
        self.assertTrue(result["transport_diagnostics"]["capacity_detected"])
        self.assertNotIn("sk-test-secret", json.dumps(result))

    def test_failure_incomplete_tools_and_invalid_events_never_succeed(self):
        """失败终态、EOF、工具调用及格式异常都不能把部分回答当成功结果。"""
        for scenario in ("failure", "eof", "tool", "approval", "bad_json", "bad_usage"):
            with self.subTest(scenario=scenario), self.assertRaises(llm.LLMError) as error:
                self.run_sample(scenario)
            self.assertNotIn("sk-test-secret", str(error.exception))

    def test_timeout_and_cancel_also_cover_handshake(self):
        """初始化卡住和生成卡住都能迅速取消或超时，不能等待后台常驻进程退出。"""
        for scenario in ("hang_init", "hang_turn"):
            with self.subTest(scenario=scenario):
                started = time.monotonic()
                with self.assertRaisesRegex(llm.LLMError, "timeout"):
                    self.run_sample(scenario, timeout=.25)
                stop = threading.Event()
                timer = threading.Timer(.25, stop.set)
                timer.start()
                try:
                    with self.assertRaises(llm.LLMCancelled):
                        self.run_sample(scenario, stop=stop)
                finally:
                    timer.cancel()
                    timer.join()
                self.assertLess(time.monotonic() - started, 2)

    def test_missing_usage_and_reroute_are_reported_honestly(self):
        """无用量不估算 token；上游换模型时保留实际模型名。"""
        result = self.run_sample("no_usage")
        self.assertIsNone(result["usage"]["completion_tokens"])
        self.assertEqual(self.run_sample("reroute")["response_model"], "gpt-other")

    def test_desktop_only_login_retains_isolated_exec(self):
        """没有本站账号时沿用隔离 exec，不能加载桌面自定义配置来凑首字指标。"""
        with patch.object(codex_stream.codex_auth, "selected_home", return_value=None), \
                patch.object(codex_stream.channel, "complete", return_value={"ttft": None}) as legacy:
            self.assertEqual(codex_stream.complete("gpt-test", "test", 1)["transport"], "codex-cli")
            legacy.assert_called_once()

    def test_probe_uses_stream_for_performance_and_persists_ttft(self):
        """测速调度走流式适配器，历史记录保留实测首字及正确的渠道标记。"""
        manager = codex_probe.Probe()
        sample = {"completed": True, "transport": "codex-app-server", "ttft": .5,
                  "text": "sample " * 200, "usage": {"completion_tokens": 500},
                  "performance": {"total_seconds": 4, "response_seconds": 3}}
        with patch.object(codex_stream.channel, "catalog", return_value={"ready": True, "models": ["gpt-test"]}), \
                patch.object(codex_stream, "complete", return_value=sample) as complete:
            manager.start("gpt-test", "performance")
            deadline = time.monotonic() + 3
            while manager.busy() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(manager.busy())
            complete.assert_called_once()
        row = manager.snapshot()["rows"][0]
        self.assertEqual(row["metrics"]["ttft_seconds"], .5)
        self.assertEqual(row["transport"], "codex-app-server")


if __name__ == "__main__":
    unittest.main()
