"""SVG 官方入口的调度、指标口径与失败持久化回归，不发送真实模型请求。"""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bench
import codex_channel
import codex_stream
import llm
import report


class OfficialSVGTests(unittest.TestCase):
    """新入口不影响普通 API，不覆盖历史记录，也不制造缺失指标。"""

    def setUp(self):
        """准备已清洗可渲染的 SVG 及代表含推理 token 的官方响应。"""
        self.site = {"name": codex_channel.SITE, "base_url": codex_channel.BASE_URL}
        self.cfg = {"prompt": "draw SVG", "timeout_seconds": 180}
        self.response = {
            "text": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
                    '<circle cx="5" cy="5" r="4"/><!--' + "a" * 1200 + '--></svg>',
            "completed": True, "error": None, "transport": "codex-app-server",
            "ttft": 2., "ttfb": None, "total": 5., "chunks": 2, "reasoning_chars": None,
            "finish_reason": "turn/completed", "truncated": False,
            "usage": {"prompt_tokens": 10, "completion_tokens": 500, "total_tokens": 510,
                      "reasoning_tokens": 100, "estimated": False},
            "performance": {"total_seconds": 5.1, "response_seconds": 5.},
            "transport_diagnostics": {"error_events": 0, "categories": [], "capacity_detected": False},
        }

    def evaluate(self, response=None, error=None):
        """只替换远端边界，使用真实跑批组装与 SVG 清洗代码。

        Args:
            response: 可选的正式响应结构，缺省使用成功 fixture。
            error: 可选的适配器异常，用于模拟失败。
        Returns:
            真实跑批结果，所有用量来自测试 fixture 而非估算。
        """
        with patch.object(codex_channel, "execution", return_value={"transport": "codex-cli"}), \
                patch.object(codex_stream, "complete", return_value=response or self.response, side_effect=error) as stream, \
                patch.object(llm, "stream_chat") as old:
            result = bench._run_one(self.cfg, self.site, self.site["name"], "gpt-test")
            stream.assert_called_once_with("gpt-test", "draw SVG", 180)
            old.assert_not_called()
            return result

    def test_svg_uses_stream_and_correct_timing_basis(self):
        """SVG 首字来自真实增量；含推理 token 的吞吐除以整轮耗时。"""
        result = self.evaluate()
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["ttft"], 2)
        self.assertIsNone(result["ttfb"])
        self.assertEqual(result["output_tps"], 100)
        self.assertEqual(result["output_tps_basis"], "end_to_end")
        self.assertEqual(result["execution"]["transport"], "codex-app-server")
        self.assertIn("（端到端）", report._table([result]))

    def test_legacy_and_missing_usage_never_fabricate_ttft_or_speed(self):
        """桌面回退不伪造首字，未返回用量时不产生吞吐。"""
        response = copy.deepcopy(self.response)
        response.update(transport="codex-cli", ttft=None)
        self.assertIsNone(self.evaluate(response)["ttft"])
        response["usage"].update(prompt_tokens=None, completion_tokens=None, total_tokens=None)
        result = self.evaluate(response)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["output_tps"])

    def test_failure_persists_only_safe_observations(self):
        """失败保留已观测首字与安全诊断，原始答案为空且没有吞吐或成绩。"""
        diagnostic = {"error_events": 1, "categories": ["所选模型容量不足 [capacity]"],
                      "capacity_detected": True}
        error = codex_channel.CodexRequestError("Codex 流式请求未成功完成 [capacity]",
                                               "codex-app-server", diagnostic, ttft=.4, chunks=1)
        result = self.evaluate(error=error)
        self.assertFalse(result["ok"])
        self.assertEqual(result["ttft"], .4)
        self.assertEqual(result["transport_diagnostics"], diagnostic)
        self.assertEqual(result["raw"], "")
        self.assertIsNone(result["output_tps"])
        # 与生产持久化结构一致地 round-trip，确认 JSON 序列化不丢诊断或时间。
        restored = json.loads(json.dumps(result))
        self.assertEqual(restored, result)

    def test_plain_api_remains_on_existing_path(self):
        """普通 HTTP 渠道继续使用原有 API 参数及首字后的输出速率口径。"""
        site = {"name": "test", "base_url": "https://example.com/v1", "api_key": "test-only"}
        with patch.object(llm, "stream_chat", return_value=self.response) as api, \
                patch.object(codex_stream, "complete") as stream:
            result = bench._run_one(self.cfg, site, site["name"], "model")
        self.assertTrue(result["ok"])
        api.assert_called_once()
        stream.assert_not_called()
        self.assertEqual(result["output_tps_basis"], "after_first_token")

    def test_failed_diagnostics_remain_escaped_in_report(self):
        """报告渲染不信任错误文本，防止协议诊断变成可执行 HTML。"""
        result = bench._failure("site", "model", '<script>alert("test")</script>', 1.)
        self.assertNotIn('<script>', report._table([result]))
        self.assertNotIn('<script>', report._card(result))


if __name__ == "__main__":
    unittest.main()
