"""仅向本机模拟模型发请求，验证安装入口、实例复用、权限检查和历史持久化。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "bin/pelican.py"


class DistributionTests(unittest.TestCase):
    """使用独立目录与系统分配端口，不访问开发者已有服务、配置或模型。"""

    def setUp(self):
        """为每个测试创建独立目录；所有临时进程由测试持有并精确清理。"""
        self.directory = tempfile.TemporaryDirectory(prefix="pelican distribution ")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
        self.data = self.home / "user data"
        self.env = {**os.environ, "CODEX_HOME": str(self.home / "empty-codex"),
                    "PELICAN_BENCH_DATA_DIR": str(self.data), "PYTHONDONTWRITEBYTECODE": "1"}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def command(self, *args):
        """运行短命令并保留失败诊断；参数直接传给解释器，不经过 shell。

        Args:
            args: pelican-bench CLI 参数；不会触发付费模型调用。
        Returns:
            子进程完成结果。
        """
        return subprocess.run([sys.executable, str(ENTRY), *args], env=self.env,
                              cwd=self.home, capture_output=True, text=True, timeout=30)

    def launch(self, node_entry=None):
        """启动 Python 或已安装的 npm 入口，核对实例 ID 后返回。

        Args:
            node_entry: 已安装 npm 包的入口路径；为空时直接使用 Python 入口。
        Returns:
            本次子进程、回环地址和已验证的运行时元数据。
        """
        log = open(self.home / "server.log", "ab")
        self.addCleanup(log.close)
        # CI 冷启动较慢时保留线程栈，区分解释器导入与 HTTP 未就绪，避免只得到空日志。
        bootstrap = ("import faulthandler,runpy,sys; "
                     "faulthandler.dump_traceback_later(30,repeat=True); "
                     "entry=sys.argv.pop(1); runpy.run_path(entry,run_name='__main__')")
        # 两个入口必须共用同一个锁；不能仅分别验证启动后就假定跨渠道切换安全。
        entry_command = ([shutil.which("node"), str(node_entry)] if node_entry is not None else
                         [sys.executable, "-u", "-c", bootstrap, str(ENTRY)])
        process = subprocess.Popen([*entry_command, "--port", "0", "--no-browser"],
                                   cwd=self.home, env=self.env, stdout=log, stderr=log)
        self.addCleanup(self.stop, process)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail((self.home / "server.log").read_text())
            try:
                state = json.loads((self.data / "runtime.json").read_text())
                base = f'http://127.0.0.1:{state["port"]}'
                if self.request(base + "/api/runtime")["instance"] == state["instance"]:
                    return process, base, state
            except (OSError, ValueError, KeyError):
                # 持锁、写端口元数据与开始处理 HTTP 之间存在正常的启动窗口。
                pass
            time.sleep(.05)
        self.fail("Server did not become ready: " + (self.home / "server.log").read_text())

    @staticmethod
    def stop(process):
        """只停止测试创建的进程；即使断言失败也不留下临时后端。

        Args:
            process: 本测试创建的 Popen。
        """
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def request(self, url, body=None, headers=None):
        """向模拟服务或测试应用发送 JSON，绕开宿主代理。

        Args:
            url: 测试内生成的回环地址。
            body: 可选 JSON 对象，提供时发送 POST。
            headers: 可选请求头，供同源与 Host 防护验证。
        Returns:
            JSON 响应。
        """
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", **(headers or {})})
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)

    def test_help_doctor_and_invalid_options(self):
        """帮助与依赖检查不创建数据；显式目录优先，未知参数不启动服务。"""
        self.assertIn("Usage: pelican-bench", self.command("--help").stdout)
        version = json.loads((ROOT / "package.json").read_text())["version"]
        self.assertEqual(self.command("--version").stdout.strip(), version)
        info = self.command("doctor", "--json", "--data-dir", str(self.home / "override"))
        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertEqual(json.loads(info.stdout)["data_directory"], str(self.home / "override"))
        self.assertFalse(self.data.exists())
        self.assertFalse((self.home / "override").exists())
        self.assertNotEqual(self.command("--unknown").returncode, 0)
        self.assertNotEqual(self.command("--port", "65536").returncode, 0)

    @unittest.skipUnless(shutil.which("node") and shutil.which("npm"), "npm channel requires Node.js")
    def test_npm_install_and_cross_channel_single_instance(self):
        """安装真正 npm 包，验证双向复用同一实例，且只暴露 pelican-bench 命令。"""
        packed = subprocess.run(["npm", "pack", "--json", "--pack-destination", str(self.home)],
                                cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(packed.returncode, 0, packed.stderr)
        package = json.loads(packed.stdout)[0]
        manifest = json.loads((ROOT / "package.json").read_text())
        # npm 自动包含 README、LICENSE 和 package.json，其余必须精确符合发布白名单。
        self.assertEqual({item["path"] for item in package["files"]},
                         set(manifest["files"]) | {"README.md", "LICENSE", "package.json"})
        prefix = self.home / "npm installation"
        install = subprocess.run(["npm", "install", "--prefix", str(prefix), "--offline",
                                  "--ignore-scripts", "--no-audit", "--no-fund",
                                  str(self.home / package["filename"])],
                                 env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(install.returncode, 0, install.stderr)
        binary = prefix / "node_modules/.bin/pelican-bench"
        self.assertTrue(binary.exists())
        self.assertFalse((prefix / "node_modules/.bin/pelican").exists())
        version = subprocess.run([str(binary), "--version"], env=self.env,
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertEqual(version.stdout.strip(), manifest["version"])

        process, _, state = self.launch()
        repeated = subprocess.run([str(binary), "--no-browser"], env=self.env,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("Already running:", repeated.stdout)
        self.assertEqual(json.loads((self.data / "runtime.json").read_text())["instance"], state["instance"])
        self.stop(process)

        # 反向验证 npm 启动后 Python 入口也复用，防止只修好其中一个入口的判断逻辑。
        node_entry = prefix / "node_modules/pelican-bench/bin/pelican-bench.mjs"
        _, _, state = self.launch(node_entry=node_entry)
        repeated = self.command("--no-browser")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("Already running:", repeated.stdout)
        self.assertEqual(json.loads((self.data / "runtime.json").read_text())["instance"], state["instance"])

    def test_local_model_history_and_single_instance(self):
        """模拟一次流式测速，验证请求来自回环、历史可恢复且重复启动不另起服务。"""
        requests = []

        class Model(BaseHTTPRequestHandler):
            """只提供测试需要的模型目录与固定 SSE，不转发到任何远程服务。"""

            def log_message(self, *_args):
                """测试静默访问日志，避免凭据进入测试报告。"""

            def do_GET(self):
                """返回固定模型目录，不读取运行机模型配置。"""
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[{"id":"mock-model"}]}')

            def do_POST(self):
                """提供足够长度的流式正文与真实测试用量，覆盖吞吐计算路径。"""
                requests.append((self.client_address[0], self.headers.get("Authorization")))
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                first = {"choices": [{"delta": {"content": "Local testing uses this computer. " * 60}}]}
                self.wfile.write(("data: " + json.dumps(first) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(.05)
                last = {"choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 32, "completion_tokens": 400, "total_tokens": 432}}
                self.wfile.write(("data: " + json.dumps(last) + "\n\ndata: [DONE]\n\n").encode())

        model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        self.addCleanup(model.server_close)
        self.addCleanup(model.shutdown)
        threading.Thread(target=model.serve_forever, daemon=True).start()
        process, base, state = self.launch()
        with self.opener.open(base + "/static/capacity.js") as response:
            self.assertEqual(response.status, 200)
        for url, body, headers in ((base + "/api/runtime", None, {"Host": "untrusted.example"}),
                                   (base + "/api/config", {}, {"Origin": "https://untrusted.example"})):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.request(url, body, headers)
            self.assertEqual(failure.exception.code, 403)
        config = self.request(base + "/api/config", {"sites": [{"name": "mock-site",
                              "base_url": f"http://127.0.0.1:{model.server_port}/v1", "api_key": "local-test-secret"}]},
                              {"Origin": base})
        self.assertNotIn("local-test-secret", json.dumps(config))
        repeated = self.command("--port", "0", "--no-browser")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("Already running:", repeated.stdout)
        self.assertEqual(json.loads((self.data / "runtime.json").read_text())["pid"], state["pid"])
        collision = self.command("--port", str(state["port"]), "--data-dir", str(self.home / "other"), "--no-browser")
        self.assertNotEqual(collision.returncode, 0)
        self.assertIn("Port is already in use", collision.stderr)
        self.request(base + "/api/probe", {"mode": "performance", "targets": [{"site": "mock-site", "model": "mock-model"}],
                     "concurrency": 2}, {"Origin": base})
        result = {}
        for _ in range(200):
            result = self.request(base + "/api/probe")
            if result.get("status") == "completed":
                break
            time.sleep(.05)
        self.assertEqual(result.get("status"), "completed", result)
        self.assertEqual(result["rows"][0]["status"], "success", result)
        self.assertEqual(result["rows"][0]["metrics"]["output_tokens"], 400)
        self.assertEqual(requests, [("127.0.0.1", "Bearer local-test-secret")])
        self.assertEqual((self.data / "config.json").stat().st_mode & 0o777, 0o600)
        self.stop(process)
        _, base, new_state = self.launch()
        self.assertNotEqual(new_state["instance"], state["instance"])
        self.assertEqual(self.request(base + "/api/probe/history")["total"], 1)
        self.assertEqual(self.request(base + "/api/probe/compare")["total"], 1)
        for name in ("config.json", ".local", "iq-runs", "__pycache__"):
            self.assertFalse((ROOT / name).exists(), f"Install tree must remain clean: {name}")


if __name__ == "__main__":
    unittest.main()
