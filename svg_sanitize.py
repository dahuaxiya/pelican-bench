"""从模型输出里抽出 SVG 并做安全清洗。

为什么必须清洗：报告页和卡片墙要的是"能看到图"，所以 SVG 必然是以真实标签形式
插进 DOM 的，不能转义。而 SVG 是活文档，<script>、on* 事件、<foreignObject>、
xlink:href="javascript:" 都能在页面里执行脚本。这些内容来自远端模型返回值，
属于不可信输入，等价于一个存储型 XSS 入口，所以插入前必须按白名单外的危险构造
逐项剥掉，而不是只信任"模型不会这么干"。

同时把外部资源引用（远程 image、@import、url(http...)）一起去掉：一是防止打开
报告时向第三方发请求泄露访问行为，二是避免离线看报告时图崩掉。

SMIL 动画（<animate>/<animateTransform>/<set>）是放行的，因为首页要求图会动。
放行的前提是按 attributeName 白名单过滤，细节见 _ANIMATABLE_ATTRS 的注释。
"""

from __future__ import annotations

import re

# 整块删掉的危险元素：脚本执行、外部文档嵌入、以及能在 SVG 里塞任意 HTML 的容器。
#
# 注意 <animate>/<animateTransform>/<set> 曾经在这个列表里，现在拿掉了：首页要求
# SVG 自己会动，而 SMIL 动画只能靠这三个标签实现，整块删等于把功能砍掉。
# 它们改走 _filter_animations() 的 attributeName 白名单——真正危险的不是"会动"，
# 而是"能改哪个属性"，所以拦截点下沉到属性级别而不是标签级别。
_DANGEROUS_ELEMENTS = (
    "script",
    "foreignObject",
    "iframe",
    "object",
    "embed",
    "audio",
    "video",
    "handler",
    "listener",
)

# SMIL 动画允许修改的属性白名单，全部小写（比较时两边都转小写）。
# 只收几何、描边、填充、透明度这类纯展示属性；凡是能把元素变成跳转或加载入口的
# （href、xlink:href、src、style、class 以及各种 on* ）一律不放进来。
#
# 为什么必须卡这一层：典型绕过是 <set attributeName="href" to="javascript:alert(1)"/>。
# 标签本身没有危险字面量，_BAD_URI_RE / _EVENT_ATTR_RE 只看标签上写死的属性值，
# 拦不住这种"运行时再把静态元素改成可执行链接"的延迟赋值。
# 用白名单而不是黑名单，是因为 SVG 可动画属性会随浏览器演进增加，
# 黑名单漏一个就等于开一个口子。
_ANIMATABLE_ATTRS = frozenset(
    {
        "transform", "gradienttransform", "patterntransform",
        "opacity", "fill", "fill-opacity", "fill-rule",
        "stroke", "stroke-opacity", "stroke-width", "stroke-dasharray",
        "stroke-dashoffset", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
        "x", "y", "dx", "dy", "x1", "y1", "x2", "y2",
        "cx", "cy", "r", "rx", "ry", "width", "height",
        "d", "points", "offset", "rotate", "startoffset",
        "stop-color", "stop-opacity", "color",
        "visibility", "display", "paint-order",
        "font-size", "font-weight", "letter-spacing", "word-spacing",
        "text-anchor", "baseline-shift",
        "filter", "mask", "clip-path",
        "marker-start", "marker-mid", "marker-end",
    }
)

# 需要按 attributeName 过滤的动画元素。
# (?![A-Za-z]) 不能省：\b 在 "<animate" 和 "<animateTransform" 之间不成立，
# 用 \b 会让 animate 这一项顺带匹配 animateTransform 和 animateMotion。
# animateMotion 不在这里，它只能沿路径移动、改不了任意属性，没有这条攻击面。
_ANIM_TAG_RE = re.compile(r"<(?:animate|animateTransform|set)(?![A-Za-z])[^>]*>", re.IGNORECASE)
_ANIM_PAIR_RE = re.compile(
    r"<(animate|animateTransform|set)(?![A-Za-z])[^>]*>\s*</\1\s*>",
    re.IGNORECASE,
)
_ATTR_NAME_RE = re.compile(
    r"\battributeName\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>/]+))",
    re.IGNORECASE,
)

# 判断"这张图会不会自己动"的两条路径，和清洗规则保持一致：
# 1. SMIL：animate / animateTransform / animateMotion 三个能自走时间轴的元素。
#    <set> 故意不算，它只是到点把属性换个值，画面并不动。
# 2. CSS：style 标签没被当成危险元素清掉，所以 @keyframes 这条路也是通的。
#    只认 @keyframes 不认 animation:，因为没有关键帧定义的 animation 声明跑不起来。
_HAS_SMIL_RE = re.compile(r"<animate(?:Transform|Motion)?(?![A-Za-z])", re.IGNORECASE)
_HAS_CSS_ANIM_RE = re.compile(r"@keyframes", re.IGNORECASE)

# on 开头的事件属性，任意标签上都可能出现。
_EVENT_ATTR_RE = re.compile(r"\son[a-zA-Z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)

# 危险协议的 href/src。javascript: 和 data:text/html 都能拿到脚本执行。
_BAD_URI_RE = re.compile(
    r"\s(?:xlink:href|href|src)\s*=\s*(\"|')?\s*(?:javascript|vbscript|data)\s*:[^\"'>]*(\"|')?",
    re.IGNORECASE,
)

# 指向 http/https/协议相对地址的外部引用，去掉以避免打开报告时外连。
_EXTERNAL_URI_RE = re.compile(
    r"\s(?:xlink:href|href|src)\s*=\s*(\"|')\s*(?:https?:)?//[^\"']*\1",
    re.IGNORECASE,
)

# CSS 里的外部拉取：@import 和 url(http...)。
_CSS_IMPORT_RE = re.compile(r"@import[^;]*;?", re.IGNORECASE)
_CSS_EXTERNAL_URL_RE = re.compile(r"url\(\s*['\"]?\s*(?:https?:)?//[^)]*\)", re.IGNORECASE)

# 开标签不能跨过另一个标签；否则推理文字中的「以 `<svg` 开头」也会被当成图。
_SVG_BLOCK_RE = re.compile(r"<svg(?=[\s>])[^<>]*>.*?</svg\s*>", re.IGNORECASE | re.DOTALL)


def extract_svg(text: str) -> str:
    """从模型返回的文本里抽出 SVG 源码。

    兼容 Markdown 围栏和混在正文里的推理文字。部分中转把 GLM 推理写入 content，
    其中可能先提到 SVG 标签或草稿，最终成图在末尾，因此选择最后一个完整候选。
    抽取不代替安全清洗，调用方仍必须调用 sanitize 后才能插入页面。

    @param text 模型返回的完整文本
    @return 最后一个完整 SVG 候选；未找到时返回空串
    """
    if not text:
        return ""
    # 先剥 markdown 围栏，避免围栏里的反引号混进 SVG 尾部。
    stripped = re.sub(r"^\s*```[a-zA-Z]*\s*", "", text.strip())
    stripped = re.sub(r"\s*```\s*$", "", stripped)
    matches = list(_SVG_BLOCK_RE.finditer(stripped))
    return matches[-1].group(0) if matches else ""


def _filter_animations(svg: str) -> tuple[str, int]:
    """按 attributeName 白名单过滤 SMIL 动画元素。

    留下改几何、颜色、透明度的动画（轮子转、脚踏板动都靠这些），
    删掉试图改 href / style / class 这类能造出执行入口的动画。

    两趟处理是为了不误伤：第一趟只吃 <set ...></set> 这种成对但内容为空的写法，
    open/close 一起删或一起留；第二趟处理自闭合和裸开标签。
    没有用 "开标签 .*? 闭标签" 的贪婪跨行匹配，否则
    <animate a/><circle/><animate b></animate> 会把中间的 <circle> 一起吞掉。

    极少数情况下（开标签和闭标签中间还夹着别的元素）会留下一个孤立的
    </animate>，浏览器会忽略无法配对的闭标签，不影响渲染也没有安全影响。

    @param svg 已删过危险元素的 SVG 源码
    @return (过滤后的源码, 被删掉的动画元素个数)
    """
    removed = 0

    def _allowed(open_tag: str) -> bool:
        match = _ATTR_NAME_RE.search(open_tag)
        if not match:
            # 没写 attributeName 的动画本身就是畸形写法，目标属性不明，按宁可多删处理。
            return False
        target = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        # 统一转小写再比：SVG 属性名本身区分大小写，但这里是做拦截判断，
        # 转小写只会让 HREF、XLINK:HREF 这类大小写绕过同样落在白名单之外。
        return target.lower() in _ANIMATABLE_ATTRS

    def _drop_pair(match: re.Match[str]) -> str:
        nonlocal removed
        whole = match.group(0)
        open_tag = whole[: whole.index(">") + 1]
        if _allowed(open_tag):
            return whole
        removed += 1
        return ""

    def _drop_single(match: re.Match[str]) -> str:
        nonlocal removed
        open_tag = match.group(0)
        if _allowed(open_tag):
            return open_tag
        removed += 1
        return ""

    cleaned = _ANIM_PAIR_RE.sub(_drop_pair, svg)
    cleaned = _ANIM_TAG_RE.sub(_drop_single, cleaned)
    return cleaned, removed


def sanitize(svg: str) -> tuple[str, list[str]]:
    """清洗 SVG，返回可安全插入 DOM 的源码和被清理项说明。

    清洗顺序是刻意的：先整块删危险元素（连内容一起删），再删属性，
    最后处理 CSS。反过来做的话，先删属性会留下空的 <script> 壳，
    内容还在 DOM 里。

    注意这里用正则而非 XML 解析：模型产出的 SVG 经常不是严格合法 XML
    （属性没引号、标签没闭合），严格解析会把本来能渲染的图判成失败。
    代价是正则清洗要按"宁可多删"的原则来，所以危险元素是整块删而不是只删标签。

    @param svg 原始 SVG 源码
    @return (清洗后的 SVG, 清理说明列表)；输入为空时返回空串和空列表
    """
    if not svg:
        return "", []

    notes: list[str] = []
    cleaned = svg

    for tag in _DANGEROUS_ELEMENTS:
        # 成对标签连内容一起删。
        paired = re.compile(rf"<{tag}\b.*?</{tag}\s*>", re.IGNORECASE | re.DOTALL)
        cleaned, hits = paired.subn("", cleaned)
        # 自闭合或未闭合的残留单标签也要删，否则 <script src=...> 这种还在。
        single = re.compile(rf"<{tag}\b[^>]*/?>", re.IGNORECASE)
        cleaned, hits2 = single.subn("", cleaned)
        if hits or hits2:
            notes.append(f"移除 <{tag}> ×{hits + hits2}")

    # 放在删元素之后、删属性之前：动画元素是整块删的，和上面的 _DANGEROUS_ELEMENTS
    # 同一层语义，只是判断条件从"标签名"换成了"目标属性名"。
    cleaned, hits = _filter_animations(cleaned)
    if hits:
        notes.append(f"移除非法目标的动画元素 ×{hits}")

    cleaned, hits = _EVENT_ATTR_RE.subn("", cleaned)
    if hits:
        notes.append(f"移除事件属性 ×{hits}")

    cleaned, hits = _BAD_URI_RE.subn("", cleaned)
    if hits:
        notes.append(f"移除危险协议链接 ×{hits}")

    cleaned, hits = _EXTERNAL_URI_RE.subn("", cleaned)
    if hits:
        notes.append(f"移除外部资源引用 ×{hits}")

    cleaned, hits = _CSS_IMPORT_RE.subn("", cleaned)
    if hits:
        notes.append(f"移除 CSS @import ×{hits}")

    cleaned, hits = _CSS_EXTERNAL_URL_RE.subn("none", cleaned)
    if hits:
        notes.append(f"移除 CSS 外部 url() ×{hits}")

    return cleaned.strip(), notes


def has_animation(svg: str) -> bool:
    """判断清洗后的 SVG 是否带自播放动画。

    必须在 sanitize() 之后调用：清洗会按 attributeName 白名单丢掉一部分动画元素，
    在清洗前判断会把"写了但被我们删掉"的图误报成动图。

    这个标记的用途是把"模型画了张静态图"和"动画在前端没跑起来"区分开。
    用户看到卡片不动时，有了这个标记就能直接定位是内容问题还是渲染问题，
    不用再去翻原始响应。

    @param svg 已经过 sanitize() 清洗的 SVG 源码，允许为空串。
    @return 含 SMIL 动画元素或 CSS @keyframes 时返回 True，否则 False。
    """
    if not svg:
        return False
    return bool(_HAS_SMIL_RE.search(svg) or _HAS_CSS_ANIM_RE.search(svg))
