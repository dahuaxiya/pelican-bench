"""从固定的官方提交制作本地题库；运行评测时不联网下载或更新题目。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import urllib.request
from runtime_paths import DATA_DIR

ROOT = DATA_DIR / "iq-data"
SOURCES = {
    "bbeh": ("google-deepmind/bbeh", "80d12ca916b7158f22293fcf3144f4d3d854d4be"),
    "arc": ("arcprize/ARC-AGI-2", "f3283f727488ad98fe575ea6a5ac981e4a188e49"),
}


def prepare(name: str, archive: Path | None = None) -> None:
    """读取固定官方归档，保留源文件并生成可校验的索引。

    Args:
        name: SOURCES 中的固定名称，不接受任意下载地址。
        archive: 已下载的同提交 tar.gz；省略时从官方 GitHub 下载。
    """
    repo, commit = SOURCES[name]
    target = ROOT / name
    target.mkdir(parents=True, exist_ok=True)
    if archive is None:
        archive = target / "source.tar.gz"
        url = f"https://codeload.github.com/{repo}/tar.gz/{commit}"
        with urllib.request.urlopen(url, timeout=120) as response:
            archive.write_bytes(response.read())
    files = {}
    with tarfile.open(archive) as source:
        prefix = repo.split("/")[-1] + "-" + commit + "/"

        def read(path: str) -> bytes:
            """按固定成员名读取文件，不把归档路径解压到文件系统。

            Args:
                path: 官方归档中相对于提交目录的成员名。
            Returns:
                成员的原始字节；缺失时停止安装。
            """
            member = source.extractfile(prefix + path)
            if member is None:
                raise ValueError(f"归档缺少 {path}")
            return member.read()

        def save(local: str, original: str) -> bytes:
            """保存未改动的源文件，并把校验值记入版本清单。

            Args:
                local: 安装器指定的本地相对文件名。
                original: 官方归档中相对于提交目录的成员名。
            Returns:
                保存的原始字节，供后续构造题目索引。
            """
            data = read(original)
            (target / local).write_bytes(data)
            files[local] = hashlib.sha256(data).hexdigest()
            return data

        save("LICENSE", "LICENSE")
        if name == "bbeh":
            save("README.md", "README.md")
            save("evaluate.py", "bbeh/evaluate.py")
            mini = json.loads(save("data.json", "bbeh/mini/data.json"))["examples"]
            lookup = {}
            for member in source.getnames():
                if member.startswith(prefix + "bbeh/benchmark_tasks/") and member.endswith("/task.json"):
                    category = member.split("/")[-2]
                    for example in json.loads(read(member[len(prefix):]))["examples"]:
                        lookup.setdefault((example["input"], example["target"]), set()).add(category)
            # Mini 原文件未标类别。必须按题干和答案同时唯一匹配，禁止用关键词推测类别。
            categories = []
            for example in mini:
                matches = lookup.get((example["input"], example["target"]), set())
                if len(matches) != 1:
                    raise ValueError("BBEH Mini 的类别不能唯一匹配官方完整题库")
                categories.append(next(iter(matches)))
            if len(mini) != 460 or len(set(categories)) != 23 or any(categories.count(c) != 20 for c in categories):
                raise ValueError("BBEH Mini 数量与固定版本不符")
            data = json.dumps(categories, indent=2).encode()
            (target / "categories.json").write_bytes(data)
            files["categories.json"] = hashlib.sha256(data).hexdigest()
        else:
            save("README.md", "README.md" if prefix + "README.md" in source.getnames() else "readme.md")
            ids = save("evaluation.txt", "data/evaluation.txt").decode().split()
            if len(ids) != 120 or len(set(ids)) != 120:
                raise ValueError("ARC-AGI-2 公开评测题数与固定版本不符")
            (target / "evaluation").mkdir(exist_ok=True)
            for task_id in ids:
                if len(task_id) != 8 or any(c not in "0123456789abcdef" for c in task_id):
                    raise ValueError("ARC 题目标识无效")
                save(f"evaluation/{task_id}.json", f"data/evaluation/{task_id}.json")
    manifest = {"repository": f"https://github.com/{repo}", "commit": commit, "files": files}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"{name}: {commit}, {len(files)} files")


def main() -> None:
    """命令行只负责安装题库，不读取站点凭据或调用模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=SOURCES)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    prepare(args.name, args.archive)


if __name__ == "__main__":
    main()
