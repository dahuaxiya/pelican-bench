"""准备固定版本的公开 SWE-bench Lite 题集，不调用模型。"""
from pathlib import Path
import argparse
import hashlib
import json
import urllib.request
from runtime_paths import DATA_DIR

REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
DATASET = "princeton-nlp/SWE-bench_Lite"
ROOT = DATA_DIR / "swe-data"


def prepare(parquet: str | None = None) -> None:
    """将公开数据转换为本地固定快照；参考补丁仅供判分和环境自检。

    @param parquet 已下载的 parquet 路径；为空时从固定 revision 下载
    """
    import pyarrow.parquet as pq
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = Path(parquet) if parquet else ROOT / "test.parquet"
    if not parquet:
        url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/data/test-00000-of-00001.parquet"
        with urllib.request.urlopen(url, timeout=120) as response:
            source.write_bytes(response.read())
    rows = sorted(pq.read_table(source).to_pylist(), key=lambda row: row["instance_id"])
    if len(rows) != 300 or len({row["instance_id"] for row in rows}) != 300:
        raise ValueError("SWE-bench Lite test 应有 300 个独立任务")
    body = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    (ROOT / "tasks.json").write_bytes(body)
    (ROOT / "manifest.json").write_text(json.dumps({"dataset": DATASET, "revision": REVISION,
        "split": "test", "sha256": hashlib.sha256(body).hexdigest(), "count": len(rows)}, indent=2))
    print(f"Prepared {len(rows)} tasks at {ROOT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet")
    prepare(parser.parse_args().parquet)
