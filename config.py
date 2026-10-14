"""配置读写。

配置文件里存着各站点的 api key，所以：
- 文件权限收到 0600，避免同机其他用户直接读走；
- 返回给前端的配置一律走 public_config() 做掩码；
- 前端保存时如果某站点的 key 传空字符串，视为"沿用原值"，这样用户在设置页
  只改单价、不重填 key 也不会把 key 清掉。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any
from runtime_paths import CODE_DIR, DATA_DIR

ROOT = str(DATA_DIR)
SOURCE_ROOT = str(CODE_DIR)
CONFIG_PATH = os.path.join(ROOT, "config.json")
RUNS_DIR = os.path.join(ROOT, "runs")

# 默认提示词。刻意写得很死，是因为要把"模型画得好不好"和"模型听不听话"分开：
# 只要它多输出一句解释或包一层 markdown，卡片墙就会渲染不出来，
# 那属于指令遵循问题而不是画图能力问题，所以这里把格式约束写到最明确。
#
# 动画部分明确要求用 SMIL（<animate>/<animateTransform>），不写 CSS @keyframes 也不写 JS：
# JS 会被 svg_sanitize 整块删掉；CSS 动画本身可用，但模型常把 @keyframes 写在 <style> 里，
# 首页是多张图同屏，动画名容易互相覆盖。SMIL 写在元素内部，天然不串台。
DEFAULT_PROMPT = (
    "Draw a pelican riding a bicycle as a single self-contained ANIMATED SVG illustration.\n"
    "\n"
    "Requirements:\n"
    "- Output ONLY the SVG code. No markdown code fences, no explanation, no preamble.\n"
    "- Start your response with <svg and end it with </svg>.\n"
    "- Include a viewBox attribute so it scales.\n"
    "- Use only inline attributes or an inline <style> block. No external resources.\n"
    "- No <script>, no <foreignObject>, no external images.\n"
    "- Make it recognisable: the pelican (with its distinctive pouched beak) should be\n"
    "  sitting on the bicycle, and the bicycle should have two wheels, a frame,\n"
    "  handlebars and pedals.\n"
    "- It MUST animate by itself, looping forever. Use SMIL animation elements\n"
    "  (<animate>, <animateTransform>) nested inside the shapes they animate.\n"
    "  Do not use CSS @keyframes and do not use JavaScript.\n"
    "- Animate at least: both wheels spinning, the pedals/crank turning, and the\n"
    "  pelican bobbing or its beak/wings moving.\n"
    "- Every animation must set repeatCount=\"indefinite\" so it never stops.\n"
    "- Animations may only target presentation attributes such as transform, cx, cy,\n"
    "  x, y, r, opacity, fill and stroke. Never animate href, xlink:href or style.\n"
)

DEFAULTS: dict[str, Any] = {
    "prompt": DEFAULT_PROMPT,
    "concurrency": 4,
    "timeout_seconds": 180,
    "max_tokens": 8000,
    "temperature": 0.7,
    "pricing": {},
    "iq_auto_review_site": "codeproxy",
    "iq_auto_review_model": "gpt-6.1-sol",
    "sites": [],
}

# 保存普通设置与保存复核设置必须共用同一把读改写锁，否则会覆盖彼此的新值。
# 使用可重入锁，让持锁的更新操作仍能调用 load/save，而不会自己等待自己。
_LOCK = threading.RLock()


def load(*, include_official: bool = True) -> dict[str, Any]:
    """读取配置，缺失的键用默认值补齐。

    配置文件不存在时返回一份纯默认配置，而不是抛错，这样第一次启动可以直接进
    设置页填站点，不需要用户先手动拷一份 config.example.json。

    @param include_official 普通站点目录查询可关闭账号渠道探测；其他调用保持默认
    @return 补齐默认值后的配置字典，调用方可以安全地直接索引已知键
    """
    raw: dict[str, Any] = {}
    if os.path.exists(CONFIG_PATH):
        with _LOCK:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                loaded = json.load(fh)
        # 只接受对象形式的配置；顶层是数组或标量时按空配置处理，避免后续 .get 崩掉。
        if isinstance(loaded, dict):
            raw = loaded

    cfg = dict(DEFAULTS)
    cfg.update(raw)
    # prompt 允许在文件里写成空串表示"用默认提示词"，省得用户为了恢复默认去删字段。
    if not str(cfg.get("prompt") or "").strip():
        cfg["prompt"] = DEFAULT_PROMPT
    import official_channels
    cfg["sites"] = [_normalise_site(s) for s in cfg.get("sites") or [] if isinstance(s, dict)
                    and not official_channels.reserved(s)]
    if include_official:
        cfg["sites"].extend(channel.site_config() for channel in official_channels.adapters())
    cfg["pricing"] = _normalise_pricing(cfg.get("pricing"))
    return cfg


def save(cfg: dict[str, Any]) -> None:
    """把配置写回磁盘。

    先写临时文件再 rename，保证中途失败不会留下半个被截断的 config.json
    （里面含 key，被写坏就得重新填一遍）。

    @param cfg 完整配置字典，调用方需自行保证 api key 已经是真实值而非掩码
    """
    import official_channels
    # 内置账号渠道不落入用户 API 配置，旧服务也不会误把登录态当作 HTTP 密钥使用。
    cfg = {**cfg, "sites": [s for s in cfg.get("sites", [])
                           if not official_channels.reserved(s)]}
    with _LOCK:
        os.makedirs(ROOT, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        # 配置含明文 key，落盘后立刻收紧到仅所有者可读写。
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)


def save_review_settings(data: dict[str, Any]) -> dict[str, str]:
    """只更新复核站点和模型，不请求上游，也不改写其他设置或凭据。

    Args:
        data: 只含 iq_auto_review_site 和 iq_auto_review_model 的对象。
            站点须已配置；模型允许不在缓存目录中，但必须是非空字符串。
    Returns:
        已落盘的两个公开字段。非法选择抛出 ValueError，原文件不变。
    """
    fields = {"iq_auto_review_site", "iq_auto_review_model"}
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("只允许设置复核站点和模型")
    selected = {}
    for field, label in (("iq_auto_review_site", "复核站点"), ("iq_auto_review_model", "复核模型")):
        value = data[field]
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise ValueError(label + "必须是 1 到 256 个字符的非空名称")
        selected[field] = value.strip()
    with _LOCK:
        cfg = load()
        # 仅按服务端站点名寻址；保存不查 /models，避免目录缓存缺失挡住合法选择。
        if not any(s["name"] == selected["iq_auto_review_site"] and s.get("base_url") for s in cfg["sites"]):
            raise ValueError("找不到已配置的复核站点：" + selected["iq_auto_review_site"])
        cfg.update(selected)
        save(cfg)
    return selected


def _normalise_site(site: dict[str, Any]) -> dict[str, Any]:
    """把单个站点配置补成统一结构。

    base_url 统一去掉结尾斜杠，后面拼接 /models、/chat/completions 时就不用再考虑
    用户填的是 ".../v1" 还是 ".../v1/"。

    @param site 原始站点配置片段
    thinking_mode 默认沿用站点设置，只有显式选择时才覆盖模型推理行为。
    max_tokens 为空时使用全局上限；站点覆盖允许为无法关闭推理的上游预留额度。
    prompt 留空沿用全局提示词；站点专用提示词必须随配置保存，便于复现和公平对比。

    @return 含站点身份、模型、单价、推理模式、可选 token 上限和提示词的规整结构
    """
    models = site.get("models")
    thinking_mode = site.get("thinking_mode", "default")
    if thinking_mode not in ("default", "disabled", "enabled"):
        thinking_mode = "default"
    max_tokens = site.get("max_tokens")
    # 旧配置和留空输入都沿用全局值；非法覆盖必须报错，不能静默使用更大的额度。
    if max_tokens not in (None, ""):
        if isinstance(max_tokens, bool) or not str(max_tokens).isdigit() or not 1 <= int(max_tokens) <= 131072:
            raise ValueError("站点最大输出 token 必须是 1 到 131072 的整数，或留空")
        max_tokens = int(max_tokens)
    else:
        max_tokens = None
    return {
        "name": str(site.get("name") or "").strip(),
        "base_url": str(site.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(site.get("api_key") or ""),
        # models 只作为 /v1/models 拉取失败时的兜底候选，平时留空。
        "models": [str(m).strip() for m in models if str(m).strip()] if isinstance(models, list) else [],
        "pricing": _normalise_pricing(site.get("pricing")),
        "thinking_mode": thinking_mode,
        "max_tokens": max_tokens,
        "prompt": str(site.get("prompt") or "").strip(),
    }


def _normalise_pricing(raw: Any) -> dict[str, dict[str, float]]:
    """把单价表规整成 {模型名: {input: 每百万 token 价, output: ...}}。

    只保留能转成数字的项，非法值直接丢掉而不是报错——单价属于可选信息，
    填错一项不该让整个配置加载失败，最坏结果只是该模型不计费用。

    @param raw 原始单价表，允许为 None 或任意脏数据
    @return 规整后的单价表；无有效项时为空字典
    """
    out: dict[str, dict[str, float]] = {}
    if not isinstance(raw, dict):
        return out
    for model, prices in raw.items():
        if not isinstance(prices, dict):
            continue
        entry: dict[str, float] = {}
        for field in ("input", "output"):
            value = prices.get(field)
            try:
                if value is None or str(value).strip() == "":
                    continue
                entry[field] = float(value)
            except (TypeError, ValueError):
                # 单价填成非数字就当没填，不影响其它模型。
                continue
        if entry:
            out[str(model).strip()] = entry
    return out


def pricing_for(cfg: dict[str, Any], site: dict[str, Any], model: str) -> dict[str, float]:
    """查某个模型的单价，站点级优先于全局。

    中转站同名模型的价格和官方价常常不一样，所以站点自己配的单价必须能盖掉全局表。
    模型名大小写在不同站点间不统一，这里做不区分大小写的兜底匹配。

    @param cfg 全量配置，用于取全局单价表
    @param site 当前站点配置，用于取站点级单价表
    @param model 模型名
    @return 命中的单价字典；没配单价时返回空字典，表示该模型不参与费用估算
    """
    import official_channels
    if official_channels.adapter(site.get("base_url")):
        return {}  # 订阅额度不按同名 API 模型单价推算美元费用。
    for table in (site.get("pricing") or {}, cfg.get("pricing") or {}):
        if model in table:
            return table[model]
        lowered = {k.lower(): v for k, v in table.items()}
        if model.lower() in lowered:
            return lowered[model.lower()]
    return {}


def public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """生成可以安全发给前端的配置副本。

    api key 只回传掩码和"是否已配置"标记，真实 key 不出进程。前端据此渲染
    占位符，用户不改 key 时提交空串，由 merge_secrets 还原。

    @param cfg 含明文 key 的完整配置
    @return 深拷贝后的配置，api_key 被替换为掩码，并额外带上 default_prompt
    """
    out = json.loads(json.dumps(cfg))
    for site in out.get("sites") or []:
        key = str(site.pop("api_key", "") or "")
        site["has_key"] = bool(key)
        site["key_hint"] = _mask(key)
    # 设置页的「恢复默认提示词」要用。默认提示词会随版本变（比如后来加了动画要求），
    # 但用户存过的 prompt 不会被静默覆盖，只能让前端拿到当前默认值主动写回。
    out["default_prompt"] = DEFAULT_PROMPT
    return out


def _mask(key: str) -> str:
    """把 key 掩成 "sk-1234…abcd" 形式，只保留足够用户辨认是哪把 key 的信息。

    @param key 明文 key，可能为空
    @return 掩码串；key 为空时返回空串，过短时整体用星号代替
    """
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def merge_secrets(new_cfg: dict[str, Any], old_cfg: dict[str, Any]) -> dict[str, Any]:
    """用旧配置补回前端没有回传的 api key。

    前端拿到的是掩码，提交时对"未修改"的 key 传空串。这里按站点 name + base_url
    找回原 key。之所以不用列表下标匹配，是因为用户可能在设置页删掉或重排站点，
    下标会错位，把 A 站点的 key 套到 B 站点上。

    @param new_cfg 前端提交的配置，api_key 可能为空串
    @param old_cfg 磁盘上的旧配置，含明文 key
    @return 合并后的配置，可直接交给 save()
    """
    old_keys: dict[tuple[str, str], str] = {}
    for site in old_cfg.get("sites") or []:
        old_keys[(site.get("name", ""), site.get("base_url", ""))] = site.get("api_key", "")

    for site in new_cfg.get("sites") or []:
        if not str(site.get("api_key") or "").strip():
            ident = (site.get("name", ""), site.get("base_url", ""))
            site["api_key"] = old_keys.get(ident, "")
    return new_cfg
