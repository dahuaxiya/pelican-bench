"""npm 分发用的本机启动入口；管理实例锁和可选依赖，不处理模型凭据。"""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser

from runtime_paths import CODE_DIR, DATA_DIR


class InstanceLock:
    """同一用户数据目录只允许一个后端或安装器写入；崩溃后由操作系统释放。"""

    def __init__(self):
        """以私有权限打开固定锁文件，不删除文件以免同时启动时锁住不同 inode。"""
        DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(DATA_DIR / "runtime.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.file = os.fdopen(descriptor, "r+b")
        self.acquired = False

    def acquire(self):
        """尝试获取跨进程非阻塞锁；仅锁冲突返回 False，权限等故障向调用方报错。"""
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise

    def write(self, value):
        """仅锁持有者写公开实例信息；在端口就绪前不会对外声明实例可用。

        Args:
            value: 只包含状态、PID、回环端口和随机实例 ID 的字典。
        """
        if not self.acquired:
            raise RuntimeError("Instance lock not held")
        # Windows 字节锁会阻止其他进程读取被锁字节。元数据单独原子替换，
        # 既能在持锁期间供重复启动读取，也不会暴露写到一半的 JSON。
        temporary = DATA_DIR / "runtime.json.tmp"
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(DATA_DIR / "runtime.json")

    def existing(self):
        """校验锁中的实例确实仍在对应回环端口服务；不依赖可能已被复用的 PID。"""
        # 另一次启动可能已持锁但仍在导入模块，短暂等待元数据与监听同时就绪。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(20):
            try:
                with (DATA_DIR / "runtime.json").open(encoding="utf-8") as source:
                    data = json.loads(source.read(4096))
                port = data.get("port")
                if type(port) is int and 1 <= port <= 65535 and data.get("instance"):
                    url = f"http://127.0.0.1:{port}/"
                    with opener.open(url + "api/runtime", timeout=.3) as response:
                        live = json.load(response)
                    if live == {"application": "pelican-bench", "instance": data["instance"]}:
                        return url
            except (OSError, ValueError):
                pass
            time.sleep(.1)
        raise ValueError("This data directory is busy (starting, stopping or installing). Try again shortly.")

    def close(self):
        """关闭描述符释放锁；保留元数据供诊断，下一次获得锁即可覆盖过期状态。"""
        self.file.close()


def doctor():
    """只检查本地可执行文件和数据是否存在，不登录、推理或下载依赖。"""
    worker = DATA_DIR / ".venv-swe" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return {"python": sys.version.split()[0], "python_executable": sys.executable,
            "data_directory": str(DATA_DIR), "api_benchmarks": "ready",
            "codex_cli": shutil.which("codex"), "kimi_cli": shutil.which("kimi"),
            "docker_cli": shutil.which("docker"), "engineering_python_installed": worker.is_file(),
            "bbeh_installed": (DATA_DIR / "iq-data/bbeh/manifest.json").is_file(),
            "arc_installed": (DATA_DIR / "iq-data/arc/manifest.json").is_file(),
            "note": "CLI presence does not verify login, model access or Docker readiness."}


def setup_engineering(parquet):
    """仅在显式安装时下载固定依赖和题库，不拉任务镜像或发模型请求。

    Args:
        parquet: 可选的本机公开 SWE 数据文件；None 时安装器从固定源下载。
    """
    if os.name == "nt":
        raise ValueError("Engineering benchmarks require Linux/macOS; on Windows run inside WSL2.")
    if sys.version_info < (3, 11):
        raise ValueError("Engineering dependencies require Python 3.11+; select it with --python.")
    environment = DATA_DIR / ".venv-swe"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / "bin/python"
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(CODE_DIR / "swe-requirements.lock")], check=True)
    command = [str(python), str(CODE_DIR / "swe_setup.py")]
    if parquet:
        command.extend(["--parquet", str(parquet.resolve())])
    subprocess.run(command, check=True)


def interrupted(_signum, _frame):
    """将终止信号转成可清理的退出；参数由 signal 调用，不能来自网页。"""
    raise KeyboardInterrupt


def main():
    """执行本机命令；启动、题库准备共用目录锁，防止任务运行中替换判分资源。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="start", choices=("start", "doctor", "setup-iq", "setup-engineering"))
    parser.add_argument("dataset", nargs="?", choices=("bbeh", "arc"))
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--parquet", type=Path)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    if args.command == "setup-iq" and not args.dataset:
        parser.error("setup-iq requires bbeh or arc")
    if args.dataset and args.command != "setup-iq":
        parser.error("dataset only applies to setup-iq")
    if args.command == "doctor":
        result = doctor()
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else "\n".join(f"{key}: {value}" for key, value in result.items()))
        return
    # 不修改 HOME/CODEX_HOME 等用户设置，只为本应用新文件收紧 POSIX 默认权限。
    os.umask(0o077)
    lock = InstanceLock()
    try:
        if not lock.acquire():
            if args.command != "start":
                raise ValueError("Stop this data directory's server before installing datasets or dependencies.")
            url = lock.existing() + "#performance"
            print(f"Already running: {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return
        lock.write({"pid": os.getpid(), "state": "starting"})
        if args.command == "setup-iq":
            import iq_setup
            iq_setup.prepare(args.dataset, args.archive.resolve() if args.archive else None)
            return
        if args.command == "setup-engineering":
            setup_engineering(args.parquet)
            return
        import app
        signal.signal(signal.SIGTERM, interrupted)
        instance = uuid.uuid4().hex

        def ready(url):
            """记录已经绑定的实际端口，随后才允许重复启动复用并打开页面。"""
            from urllib.parse import urlparse
            lock.write({"pid": os.getpid(), "state": "running", "port": urlparse(url).port, "instance": instance})
            print(f"Data: {DATA_DIR}\nOpen: {url}#performance", flush=True)
            if not args.no_browser:
                webbrowser.open(url + "#performance")

        app.main(["--port", str(args.port), "--no-browser"], on_listening=ready, instance_id=instance)
    finally:
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, OSError) and error.errno == errno.EADDRINUSE:
            print("Port is already in use. Keep the existing server, or choose --port <number>.", file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        sys.exit(1)
