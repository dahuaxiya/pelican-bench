"""固定官方账号渠道注册表；评测入口不接受任意本地命令或脚本路径。"""


def adapters():
    """延迟导入两个官方渠道，避免 config、llm 与账号管理器初始化时循环依赖。"""
    import codex_channel
    import kimi_channel
    return (codex_channel, kimi_channel)


def adapter(base_url):
    """仅精确匹配内置地址；普通 HTTP 渠道继续使用原来的 API 调用。"""
    return next((item for item in adapters() if item.BASE_URL == base_url), None)


def reserved(site):
    """内置名称和地址均保留，禁止通过保存设置替换官方账号身份。"""
    return any(site.get("name") == item.SITE or site.get("base_url") == item.BASE_URL for item in adapters())
