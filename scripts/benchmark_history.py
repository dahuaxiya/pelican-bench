"""Measure restart and concurrent IQ reads against the same saved data, without model calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import time


def main():
    """Persist timing and response hashes only; compare independent interpreter runs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.source).resolve()))
    import iq
    def read(_=None):
        """Time the complete public leaderboard and hash it for semantic comparison."""
        start = time.perf_counter()
        result = iq.leaderboard(benchmark="bbeh-mini", scoring="comparison")
        elapsed = round((time.perf_counter() - start) * 1000, 2)
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {"ms": elapsed, "sha256": digest, "entries": len(result["entries"])}
    cold = read()
    warm = [read() for _ in range(3)]
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        concurrent = list(pool.map(read, range(4)))
    output = {"source": args.source, "restart_first": cold, "warm": warm,
              "concurrent": concurrent, "concurrent_wall_ms": round((time.perf_counter()-start)*1000, 2)}
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(json.dumps(output))


if __name__ == "__main__":
    main()
