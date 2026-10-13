"""Brew/npm 共用入口；管理本机实例、可选依赖和 CLI 请求。"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import shutil
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser

from runtime_paths import CODE_DIR, DATA_DIR
import cli_commands as cli


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
        """仅锁持有者写私有实例信息；在端口就绪前不会对外声明实例可用。

        Args:
            value: 状态、PID、端口、实例 ID 和仅限本机 CLI 使用的控制令牌。
        """
        if not self.acquired:
            raise RuntimeError("Instance lock not held")
        # Windows 字节锁会阻止其他进程读取被锁字节。元数据单独原子替换，
        # 既能在持锁期间供重复启动读取，也不会暴露写到一半的 JSON。
        temporary = DATA_DIR / "runtime.json.tmp"
        # stop 的控制令牌不能由网页读取，临时文件和最终文件均仅用户可读。
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            if os.name != "nt":
                os.fchmod(target.fileno(), 0o600)
            json.dump(value, target)
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


def background(port=8780):
    """启动脱离终端的本机子进程；只在随机实例校验通过后返回，不使用 PID 猜测身份。"""
    existing = cli.discover()
    if existing:
        return existing
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(DATA_DIR / "server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
    with os.fdopen(descriptor, "ab") as log:
        child = subprocess.Popen([sys.executable, "-B", "-u", str(CODE_DIR / "local_launcher.py"),
                                  "start", "--port", str(port), "--no-browser"],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 cwd=CODE_DIR, close_fds=True, **options)
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            live = cli.discover()
            if live:
                return live
            if child.poll() is not None:
                raise ValueError(f"后台启动失败，请运行 pelican-bench logs；日志：{DATA_DIR / 'server.log'}")
            time.sleep(.1)
        raise ValueError("服务启动超过 60 秒，请检查 pelican-bench logs")
    except BaseException:
        # 只清理本次创建且尚未返回给用户的子进程，不影响锁竞争中获胜的其他实例。
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise


def stop_server(wait=False, timeout=30):
    """通过已验证实例的私有控制口停止；忙碌时拒绝或有限等待，不给磁盘 PID 发信号。"""
    if timeout < 1:
        raise ValueError("--timeout 必须大于 0")
    live = cli.discover()
    if not live:
        return {"state": "stopped", "message": "本数据目录没有可验证的运行实例"}
    if not live.metadata.get("control_token"):
        raise ValueError("旧版实例不支持 stop，请在原启动终端按 Ctrl+C 后重新启动")
    deadline = time.monotonic() + timeout
    while True:
        try:
            live.request("/api/runtime/stop", {}, timeout=5)
            break
        except cli.ApiError as exc:
            if exc.status != 409 or not wait or time.monotonic() >= deadline:
                raise
            time.sleep(.5)
    # HTTP 断开不代表清理完成，必须等旧锁释放；同时保护可能刚启动的新实例。
    while time.monotonic() < deadline + 10:
        lock = InstanceLock()
        try:
            if lock.acquire():
                return {"state": "stopped"}
        finally:
            lock.close()
        current = cli.discover()
        if current and current.metadata["instance"] != live.metadata["instance"]:
            return {"state": "replaced", "message": "原实例已停止，另一个命令启动了新实例"}
        time.sleep(.1)
    raise ValueError("已发停止请求，但服务尚未退出；请检查 status 和 logs")


def main():
    """分发 CLI；只有持有目录锁的服务/安装器能写运行状态，查询不会另起执行器。"""
    parser = cli.parser()
    arguments = sys.argv[1:]
    if not arguments or (arguments[0].startswith("-") and arguments[0] not in ("--help", "-h")):
        arguments = ["start", *arguments]
    args = parser.parse_args(arguments)
    if not 0 <= getattr(args, "port", 0) <= 65535:
        parser.error("port must be between 0 and 65535")
    if getattr(args, "wait_timeout", 0) < 0:
        parser.error("--wait-timeout 不能小于 0")
    if getattr(args, "json", False) and args.command in ("start", "restart") and not args.background:
        parser.error("start/restart --json 需要 --background，避免服务日志混入 JSON")
    if args.command == "doctor":
        cli.emit(doctor(), args.json)
        return
    if args.command == "status":
        live = cli.discover()
        state = live.request("/api/runtime/status") if live else {"state": "stopped"}
        cli.emit({**state, "data_directory": str(DATA_DIR), **({"url": live.url} if live else {})}, args.json)
        return
    if args.command == "logs":
        cli.logs(args.lines, args.follow)
        return
    # 不修改 HOME/CODEX_HOME 等用户设置，只为本应用新文件收紧 POSIX 默认权限。
    os.umask(0o077)
    if args.command in ("stop", "restart"):
        stopped = stop_server(getattr(args, "wait", False), getattr(args, "timeout", 30))
        if args.command == "stop":
            cli.emit(stopped, args.json)
            return
        args.command = "start"
    if args.command == "open" or (args.command == "start" and args.background):
        live = background(getattr(args, "port", 8780))
        page = getattr(args, "page", "performance")
        url = live.url + ("/static/engineering.html" if page == "engineering" else "/#" + page)
        if not getattr(args, "no_browser", False):
            webbrowser.open(url)
        cli.emit({"state": "running", "url": url, "log": str(DATA_DIR / "server.log")}, getattr(args, "json", False))
        return
    if args.command not in ("start", "setup-iq", "setup-engineering"):
        live = cli.discover()
        if not live:
            print("正在后台启动本机服务…", file=sys.stderr)
            live = background()
        return cli.execute(args, live)
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
        control_token = secrets.token_urlsafe(32)

        def ready(url):
            """记录已经绑定的实际端口，随后才允许重复启动复用并打开页面。"""
            from urllib.parse import urlparse
            lock.write({"pid": os.getpid(), "state": "running", "port": urlparse(url).port,
                        "instance": instance, "control_token": control_token})
            print(f"Data: {DATA_DIR}\nOpen: {url}#performance", flush=True)
            if not args.no_browser:
                webbrowser.open(url + "#performance")

        app.main(["--port", str(args.port), "--no-browser"], on_listening=ready,
                 instance_id=instance, control_token=control_token)
    finally:
        if lock.acquired:
            lock.write({"state": "stopped"})
        lock.close()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, OSError) and error.errno == errno.EADDRINUSE:
            print("Port is already in use. Keep the existing server, or choose --port <number>.", file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        sys.exit(1)
