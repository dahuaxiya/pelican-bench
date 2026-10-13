"""官方推理题库适配：题目、提示词、评分器和数据提交分别留证。"""
from __future__ import annotations

import ast
import copy
from collections import Counter
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any
from runtime_paths import DATA_DIR

ROOT = DATA_DIR / "iq-data"
SUITES = ("bbeh-smoke", "bbeh-mini", "arc-smoke", "arc-agi-2")
BBEH_INSTRUCTION = '\n\nReason through the problem. End your response with "The final answer is: " followed by your answer.'
ARC_INSTRUCTION = (
    'Infer the transformation from the training input/output grids and apply it to each test input. '
    'Cells are integers 0 through 9; each output is a rectangular grid of size 1 through 30 in each dimension. '
    'Return only JSON in this format: {"predictions":[{"attempt_1":[[0]],"attempt_2":[[0]]}]}. '
    'Provide one entry per test input, in the same order. Each entry allows up to two candidate output grids; '
    'attempt_1 is required and attempt_2 is optional. No tools, Markdown, or extra fields.\n\n'
)


@lru_cache(maxsize=2)
def _source(name: str) -> dict[str, Any]:
    """启动时校验本地文件；安装缺失或内容变化时拒绝静默替换题库。

    Args:
        name: 固定 bbeh 或 arc，不能来自任意文件路径。
    Returns:
        源提交和文件哈希清单，调用方不得修改。
    """
    try:
        manifest = json.loads((ROOT / name / "manifest.json").read_text())
        for filename, digest in manifest["files"].items():
            path = (ROOT / name / filename).resolve()
            if not path.is_relative_to((ROOT / name).resolve()):
                raise ValueError("题库文件路径越界")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"题库文件校验失败：{name}/{filename}")
        return manifest
    except OSError as exc:
        raise ValueError(f"题库未安装，请运行 python3 iq_setup.py {name}") from exc


@lru_cache(maxsize=1)
def _bbeh_grader():
    """加载已校验的官方评分函数，跳过官方文件末尾的演示打印。

    Returns:
        官方 evaluate_correctness 函数；不会执行模型回答。
    """
    _source("bbeh")
    tree = ast.parse((ROOT / "bbeh/evaluate.py").read_text())
    # 执行对象是项目自带且已核验的评分器，仅加载原始函数定义。
    # 模型文本始终作为字符串参数，不进入编译、eval 或 exec。
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {}
    exec(compile(tree, "official-bbeh-evaluate.py", "exec"), namespace)
    return namespace["evaluate_correctness"]


@lru_cache(maxsize=4)
def _questions(suite: str) -> list[dict[str, Any]]:
    """构造固定题目，试跑按稳定规则取样，不按模型表现选题。

    Args:
        suite: SUITES 中的题集 ID。
    Returns:
        内部缓存；对外接口必须深拷贝，避免污染后续运行。
    """
    if suite not in SUITES:
        raise ValueError("未知的官方基准题集")
    if suite.startswith("bbeh"):
        _source("bbeh")
        raw = json.loads((ROOT / "bbeh/data.json").read_text())["examples"]
        categories = json.loads((ROOT / "bbeh/categories.json").read_text())
        seen = set()
        result = []
        for index, (example, category) in enumerate(zip(raw, categories)):
            if suite == "bbeh-smoke" and category in seen:
                continue
            seen.add(category)
            result.append({"id": f"bbeh-{index:03d}", "category": category,
                           "title": category.removeprefix("bbeh_").replace("_", " ") + f" / {index}",
                           "source_index": index, "grader": "bbeh", "expected": example["target"],
                           "input": example["input"], "prompt": example["input"] + BBEH_INSTRUCTION})
        return result
    _source("arc")
    ids = (ROOT / "arc/evaluation.txt").read_text().split()
    result = []
    for task_id in ids[:3] if suite == "arc-smoke" else ids:
        task = json.loads((ROOT / f"arc/evaluation/{task_id}.json").read_text())
        # 只能把示例答案提供给模型。测试答案与 prompt 分开保存，禁止发送整个源对象。
        visible = {"train": task["train"], "test": [{"input": row["input"]} for row in task["test"]]}
        result.append({"id": task_id, "category": "abstract_reasoning", "title": f"ARC / {task_id}",
                       "grader": "arc", "expected": [row["output"] for row in task["test"]],
                       "input": visible, "prompt": ARC_INSTRUCTION + json.dumps(visible, separators=(",", ":"))})
    return result


def questions(suite: str) -> list[dict[str, Any]]:
    """返回独立题目副本。

    Args:
        suite: 官方完整题集或试跑集标识。
    Returns:
        含服务端标准答案的列表，模型请求仅能发送 prompt 字段。
    """
    return copy.deepcopy(_questions(suite))


def manifest(suite: str) -> dict[str, Any]:
    """返回不含题目和答案的版本清单，明确试跑、正式榜及候选次数。

    Args:
        suite: 官方题集标识。
    Returns:
        可持久化、可公开的题集元数据。
    """
    tasks = _questions(suite)
    bbeh = suite.startswith("bbeh")
    source = _source("bbeh" if bbeh else "arc")
    counts = Counter(q["category"] for q in tasks)
    smoke = suite.endswith("smoke")
    return {"id": suite, "benchmark": "bbeh-mini" if bbeh else "arc-agi-2",
            "name": ("BBEH Mini" if bbeh else "ARC-AGI-2") + (" · 试跑" if smoke else " · 完整"),
            "version": source["commit"], "source": source["repository"], "count": len(tasks),
            "formal": not smoke, "metric": "accuracy", "candidates": 1 if bbeh else 2,
            "protocol": "text-no-tools-v1", "prompt_version": "bbeh-final-answer-v1" if bbeh else "arc-two-grids-v1",
            "grader_version": "bbeh-" + source["files"]["evaluate.py"] if bbeh else "arc-all-test-exact-two-candidates-v1",
            "sha256": hashlib.sha256(json.dumps(tasks, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "weights": {c: n / len(tasks) for c, n in counts.items()},
            "categories": [{"id": c, "name": c.removeprefix("bbeh_").replace("_", " "), "count": n}
                           for c, n in sorted(counts.items())],
            "description": ("23 类推理题，官方答案抽取与评分。" if bbeh else
                            "公开评测集，文本输入，每个测试网格最多两个候选，全部测试网格正确才算解出一道题。") +
                           ("试跑不进入正式榜单。" if smoke else "")}


def _grid(value: Any) -> bool:
    """检查 ARC 网格类型和尺寸；布尔值、浮点数不能冒充颜色编号。

    Args:
        value: 模型返回的候选网格。
    Returns:
        是否为 1..30 行列、颜色 0..9 的矩形整数网格。
    """
    return (type(value) is list and 1 <= len(value) <= 30 and type(value[0]) is list and
            1 <= len(value[0]) <= 30 and all(type(row) is list and len(row) == len(value[0]) and
            all(type(cell) is int and 0 <= cell <= 9 for cell in row) for row in value))


def grade(question: dict[str, Any], text: str) -> dict[str, Any]:
    """按各基准判分，BBEH 直接调用官方函数，ARC 严格检查整题网格。

    Args:
        question: 含 grader 和 expected 的固定题目。
        text: 未截断、传输完整的模型正文。
    Returns:
        0/1 得分、结果状态及判分说明。
    """
    if question["grader"] == "bbeh":
        correct = _bbeh_grader()(text, question["expected"])
        return {"score": int(correct), "status": "correct" if correct else "incorrect",
                "reason": "BBEH 官方 evaluate_correctness：" + ("通过" if correct else "未通过")}
    from iq_tasks import _unique_object
    try:
        answer = json.loads(text, object_pairs_hook=_unique_object)
        if type(answer) is not dict or set(answer) != {"predictions"}:
            raise ValueError("需要 predictions 字段")
        predictions = answer["predictions"]
        if type(predictions) is not list or len(predictions) != len(question["expected"]):
            raise ValueError("预测数量与测试输入数量不一致")
        matches = []
        for prediction, expected in zip(predictions, question["expected"]):
            if (type(prediction) is not dict or "attempt_1" not in prediction or
                    set(prediction) - {"attempt_1", "attempt_2"} or not all(_grid(g) for g in prediction.values())):
                raise ValueError("候选必须是合法整数网格，每个输入最多两个候选")
            matches.append(any(g == expected for g in prediction.values()))
    except (ValueError, TypeError, RecursionError):
        return {"score": 0, "status": "format_error", "reason": "需要完整 JSON，每个测试输入包含至多两个合法的整数网格"}
    correct = all(matches)
    return {"score": int(correct), "status": "correct" if correct else "incorrect",
            "reason": f"完全匹配 {sum(matches)}/{len(matches)} 个测试网格；全部正确才算解出本题"}
