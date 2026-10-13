#!/usr/bin/env python3
"""Homebrew 入口：使用随配方安装的 Python，所有可写数据留在用户目录。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent.parent


def default_data_directory():
    """选择与 npm 入口相同的用户目录，让两个渠道升级或切换时共用实例锁和历史。"""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/pelican-bench"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "pelican-bench"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "pelican-bench"


def main():
    """先确定数据目录再加载业务模块；不启动 shell、不读取密钥、不自动安装可选依赖。"""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--data-dir")
    parser.add_argument("--version", "-v", action="store_true")
    args, remaining = parser.parse_known_args()
    if args.version:
        print(json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"])
        return
    data = Path(args.data_dir or os.environ.get("PELICAN_BENCH_DATA_DIR") or default_data_directory()).expanduser().resolve()
    os.environ["PELICAN_BENCH_DATA_DIR"] = str(data)
    # Homebrew Cellar 随升级替换；本进程和 worker 都不能在代码目录生成运行数据或缓存。
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONUTF8"] = "1"
    sys.path.insert(0, str(ROOT))
    sys.argv = ["pelican-bench", *remaining]
    runpy.run_path(str(ROOT / "local_launcher.py"), run_name="__main__")


if __name__ == "__main__":
    main()
