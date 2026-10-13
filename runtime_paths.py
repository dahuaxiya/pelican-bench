"""区分安装资源与可写数据；源码运行保持原目录，CLI 显式指定用户目录。"""
import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
# npm/npx 的安装目录可被缓存清理或替换，任何运行数据都不能依赖它持久化。
DATA_DIR = Path(os.environ.get("PELICAN_BENCH_DATA_DIR") or CODE_DIR).expanduser().resolve()
DISTRIBUTED = bool(os.environ.get("PELICAN_BENCH_DATA_DIR"))
