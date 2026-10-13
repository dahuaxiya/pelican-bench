"""可追溯的答案规范化；只解析回答，不执行回答，也不向提取器提供正确答案。"""
from __future__ import annotations

from collections import OrderedDict
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import re
import threading
from typing import Any

VERSION = "answer-normalization-v1"
_CACHE: OrderedDict = OrderedDict()
_LOCK = threading.Lock()
_FINAL = re.compile(r"(?:the\s+)?final\s+answer\s*(?:is\s*)?[:：]?\s*|最终答案\s*(?:是)?\s*[:：]?\s*", re.I)
_ANSWER = re.compile(r"(?:the\s+)?answer\s+is\s*[:：]?\s*", re.I)
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?(?:/[+-]?\d+)?")


def unwrap(text: str) -> str:
    """去掉成对的展示包装；内部标点和词序不变。

    Args:
        text: 完整候选答案，不接受非文本值。
    Returns:
        去除外层代码围栏、Markdown、引号或简单 LaTeX 包装的文本。
    """
    text = text.strip()
    for _ in range(8):
        before = text
        fence = re.fullmatch(r"```(?:json|text|plaintext)?\s*\n(.*?)\n```", text, re.S | re.I)
        if fence:
            text = fence[1].strip()
        for left, right in (("**", "**"), ("__", "__"), ("`", "`"), ('"', '"'), ("“", "”"),
                            ("'", "'"), ("$", "$"), (r"\(", r"\)"), (r"\[", r"\]")):
            # 展示包装外的句号不属于答案；不移除包装内部的语言学符号。
            if text.startswith(left) and text.endswith(right + "."):
                text = text[:-1]
            if len(text) > len(left) + len(right) and text.startswith(left) and text.endswith(right):
                text = text[len(left):-len(right)].strip()
        latex = re.fullmatch(r"\\(?:boxed|text|texttt)\{([^{}]*)\}", text, re.S)
        if latex:
            text = latex[1].strip()
        if text == before:
            break
    return text


def final_answer(text: str) -> str:
    """取最后一个明确的最终答案段，保留该段后的内容用于冲突检查。

    Args:
        text: 模型原始正文；标准答案不得作为参数传入。
    Returns:
        有前缀时返回其后的完整内容，否则使用整个回答；不搜索推理中的正确选项。
    """
    text = unwrap(text)
    matches = list(_FINAL.finditer(text)) or list(_ANSWER.finditer(text))
    if not matches:
        return text
    match = matches[-1]
    candidate = text[match.end():].strip()
    # 最终答案标记可能位于粗体内部，切片后不能把成对的闭合符当成答案内容。
    # 只补齐跨切片边界的包装，不全局删除字符，避免改写答案语义。
    for token in ("**", "__", "`"):
        if text[:match.start()].count(token) % 2:
            if candidate.startswith(token):
                candidate = candidate[len(token):].strip()
            elif candidate.endswith(token):
                candidate = candidate[:-len(token)].strip()
    return unwrap(candidate)


@lru_cache(maxsize=1024)
def choices(question_text: str) -> dict[str, str]:
    """从题干的连续选项行读取编号和文案，重复编号或不连续编号拒绝推断。

    Args:
        question_text: 原始题干，必须没有附加标准答案。
    Returns:
        唯一选项表；不能确认选择题结构时返回空表。
    """
    matches = list(re.finditer(r"(?m)^\s*(?:\(([A-Z])\)|([A-Z])[.)])\s+", question_text))
    labels = [m[1] or m[2] for m in matches]
    if len(labels) < 2 or labels != [chr(65 + i) for i in range(len(labels))]:
        return {}
    options = {label: question_text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(question_text)].strip()
               for i, (label, m) in enumerate(zip(labels, matches))}
    # BBEH 的最后一个选项后紧跟作答要求，不能把这些要求误认为选项文案。
    options[labels[-1]] = re.split(r"\n(?:\s*\n)*(?:Provide your final answer|Coordinates have been rounded)",
                                 options[labels[-1]], maxsplit=1)[0].strip()
    return options


def _option_text(text: str) -> str:
    """比较选项文案时兼容引号排版与句末句号；不用于语言学答案。

    Args:
        text: 英文选项文案或模型重复的选项文案。
    Returns:
        保留词语、否定词和内部标点的比较字符串。
    """
    text = unwrap(text).translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'}))
    return " ".join(text.strip(' \t\n\r\"').removesuffix('.').split()).casefold()


def schema(question: dict[str, Any]) -> dict[str, Any]:
    """为每题确定答案结构；仅传递类型与题干选项，不把参考值带入提取逻辑。

    Args:
        question: 保存的题目，含类别与标准答案。
    Returns:
        可供规则或 agent 使用的类型描述，不包含标准答案值。
    """
    expected = str(question.get("expected", "")).strip()
    options = choices(str(question.get("input", "")))
    # 多选语义来自题干，而不是标准答案长度：只选中一项的题仍然是多选题。
    if options and question.get("category") == "bbeh_hyperbaton":
        return {"kind": "multi_select", "choices": options, "exclusive": "K"}
    if options and re.fullmatch(r"\(?[A-Za-z]\)?", expected):
        return {"kind": "choice", "choices": options}
    if _NUMBER.fullmatch(expected):
        return {"kind": "number"}
    category = question.get("category", "")
    if category == "bbeh_linguini":
        return {"kind": "text", "case_sensitive": True}
    if "," in expected or (expected.startswith("[") and expected.endswith("]")):
        return {"kind": "ordered_list"}
    return {"kind": "text", "case_sensitive": False}


def parse(candidate: str, spec: dict[str, Any]) -> tuple[Any, str]:
    """按声明的答案类型解析，遇到歧义返回原因而不是猜测正确值。

    Args:
        candidate: 已定位的最终答案；不会访问参考答案。
        spec: schema 返回的无答案类型描述。
    Returns:
        规范值和失败原因；失败时规范值为 None。
    """
    value = unwrap(candidate)
    kind = spec["kind"]
    if not value or len(value) > 16000:
        return None, "最终答案为空或过长，需复核"
    if kind == "multi_select":
        value = re.sub(r"^(?:options?|choices?)\s*[:：]?\s*", "", value, flags=re.I).removesuffix(".")
        # 允许 BC、B,C、(B) (C)，但不接受 or、解释句或重复选择后擅自去重。
        if not re.fullmatch(r"[A-Za-z\s,，()\[\]]+", value):
            return None, "无法提取完整多选答案"
        labels = re.findall(r"[A-Za-z]", value.upper())
        if not labels or any(label not in spec["choices"] for label in labels) or len(set(labels)) != len(labels):
            return None, "多选答案含非法或重复选项"
        if spec.get("exclusive") in labels and len(labels) > 1:
            return None, "以上都不选与其他选项冲突"
        return sorted(labels), ""
    if kind == "choice":
        options = spec["choices"]
        # 先核对完整文案，避免把以 A 开头的选项句子误当作选项 A。
        text_matches = [label for label, text in options.items() if _option_text(value) == _option_text(text)]
        if len(text_matches) == 1:
            return text_matches[0], ""
        cleaned = value.replace("**", "").replace("`", "")
        match = re.match(r"^(?:(?:option|choice|选项)\s*)?(?:\(([A-Za-z])\)|([A-Za-z])(?=$|[\s.:：,、—–-]))", cleaned, re.I)
        if not match or (label := (match[1] or match[2]).upper()) not in options:
            return None, "无法提取唯一合法选项"
        suffix = cleaned[match.end():].strip().lstrip(".:：—–- ")
        if suffix.startswith("(") and suffix.endswith(")"):
            suffix = suffix[1:-1].strip()
        if not suffix or _option_text(suffix) == _option_text(options[label]):
            return label, ""
        # 不丢弃任意后缀：它可能给出第二个候选或否定前面的选项。
        return None, "选项后的内容未能与该选项文案唯一对应，需复核"
    if kind == "number":
        value = value.removesuffix('.')
        if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", value):
            value = value.replace(",", "")
        if len(value) > 1024 or not _NUMBER.fullmatch(value):
            return None, "最终答案无法解析为单一数值"
        try:
            # 使用精确有理数，不用 float 容差或 eval；指数长度已限制，避免异常大整数。
            return str(Fraction(value)), ""
        except (ValueError, ZeroDivisionError):
            return None, "数值格式非法"
    if kind == "ordered_list":
        value = value.removesuffix(".")
        if value.startswith("["):
            try:
                decoded = json.loads(value)
                if not isinstance(decoded, list) or any(type(x) not in (str, int) for x in decoded):
                    return None, "列表必须由字符串或整数构成"
                values = [str(x) for x in decoded]
            except ValueError:
                if not value.endswith("]"):
                    return None, "列表未闭合"
                values = value[1:-1].split(",")
        else:
            values = value.split(",")
        values = [unwrap(x.strip()).casefold() for x in values]
        if any(not x or "\n" in x or len(x) > 160 for x in values):
            return None, "列表包含空项、解释或非法元素"
        return values, ""
    if "\n" in value or len(value) > 1000:
        return None, "文本答案包含多段内容，需复核"
    return value if spec.get("case_sensitive") else value.removesuffix(".").casefold(), ""


def grade(question: dict[str, Any], text: str) -> dict[str, Any]:
    """计算独立规范化分，不替换官方评分器或用 agent 推断答案。

    Args:
        question: 原始题目和参考答案；参考值仅用于最终比较。
        text: 完整、未截断的原回答。
    Returns:
        含提取值、判分依据和规则版本的独立结果；无法解析时 score 为 None。
    """
    import iq_tasks
    base = {"version": VERSION, "extracted": None, "score": None, "status": "review_required"}
    if question.get("grader") == "arc":
        value = unwrap(text)
        result = iq_tasks.grade(question, value)
        if result["status"] == "format_error":
            return {**base, "reason": result["reason"]}
        return {**base, **result, "extracted": value}
    if question.get("grader") != "bbeh":
        return {**base, **iq_tasks.grade(question, text), "reason": "该题保留原有结构与指令遵循要求"}
    spec = schema(question)
    extracted, error = parse(final_answer(text), spec)
    reference, reference_error = parse(str(question["expected"]), spec)
    if error or reference_error:
        return {**base, "extracted": extracted, "reason": error or "该题参考结构暂不支持规范化"}
    correct = extracted == reference
    return {**base, "extracted": extracted, "score": int(correct),
            "status": "correct" if correct else "incorrect",
            "reason": "最终答案规范化后" + ("与标准答案一致" if correct else "与标准答案不同")}


def result_grade(question: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """缓存完整回答的离线判分；传输失败、截断和空答案不得因片段命中被补成正确。

    Args:
        question: 保存的题目。
        row: 保存的原始请求结果。
    Returns:
        独立结果；缓存仅保存哈希和小结果，不长期持有原文或凭据。
    """
    if not row.get("response_ok") or row["status"] in ("truncated", "empty_answer"):
        return {"version": VERSION, "status": row["status"], "score": row.get("score"),
                "extracted": None, "reason": "沿用原请求状态，缺测、截断和空回答不做答案补全"}
    question_key = json.dumps(question, ensure_ascii=False, sort_keys=True)
    key = (hashlib.sha256(question_key.encode()).digest(), hashlib.sha256(row["raw"].encode()).digest())
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return dict(_CACHE[key])
    result = grade(question, row["raw"])
    with _LOCK:
        _CACHE[key] = result
        if len(_CACHE) > 20000:
            _CACHE.popitem(last=False)
    return dict(result)


def annotate(data: dict[str, Any]) -> None:
    """给查询快照补充规范化逐题结果和摘要，不写入或修改原始历史文件。

    Args:
        data: 已独立深拷贝、已经计算官方 summary 的批次快照。
    """
    questions = {q["id"]: q for q in data["questions"]}
    data["normalization_version"] = VERSION
    for row in data["results"]:
        row["normalized"] = result_grade(questions[row["question_id"]], row)
    for summary in data["summary"]:
        rows = [r for r in data["results"] if (r["site"], r["model"]) == (summary["site"], summary["model"])]
        pending = sum(r["normalized"]["status"] == "review_required" for r in rows)
        categories = {}
        weighted = 0.0
        for category, weight in data["suite"]["weights"].items():
            expected = summary["categories"][category]["expected"]
            correct = sum(r["normalized"]["score"] == 1 for r in rows if r["category"] == category)
            count_pending = sum(r["normalized"]["status"] == "review_required" for r in rows if r["category"] == category)
            weighted += 100 * correct / expected * weight
            categories[category] = {**summary["categories"][category], "correct": correct,
                                    "score": round(100 * correct / expected, 2) if not count_pending and
                                    summary["complete"] and summary["valid"] == summary["expected"] else None}
        complete = summary["complete"] and summary["valid"] == summary["expected"]
        score = round(weighted, 2) if complete and not pending else None
        summary["normalized"] = {"version": VERSION, "correct": sum(r["normalized"]["score"] == 1 for r in rows),
                                 "incorrect": sum(r["normalized"]["score"] == 0 for r in rows),
                                 "pending": pending, "ability_score": score,
                                 "rank_score": score if data["suite"].get("formal", True) else None,
                                 "lower_bound": round(weighted, 2) if complete else None,
                                 "recovered": sum(r.get("score") == 0 and r["normalized"]["score"] == 1 for r in rows),
                                 "categories": categories}
