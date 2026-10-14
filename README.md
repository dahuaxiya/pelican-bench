# Pelican Bench

在自己的电脑上测试模型接口，用本地网页比较首字延迟、输出速度、容量错误、推理成绩和生成结果。模型请求从使用者的电脑发出，配置、账号和成绩保存在本机。

## 安装

macOS 或 Linux 已安装 Homebrew 后，执行：

```sh
brew install dahuaxiya/tap/pelican-bench
pelican-bench
```

Homebrew 自动安装 Python，不需要 npm、uv 或手动准备 Python 环境。这里使用项目维护的 Tap，不是 Homebrew Core 收录。

从 0.2.0 起统一使用 pelican-bench 命令，不再安装 pelican 别名，避免与同名博客生成器冲突。

启动后自动打开本地网页，默认端口 8780。在设置中填写自己的模型地址和凭据，再选择模型开始测试。

```sh
pelican-bench --version
pelican-bench doctor
pelican-bench --no-browser
pelican-bench --port 9000
pelican-bench --data-dir ./my-bench-data
```

同一数据目录只运行一个后端。再次输入 pelican-bench 会打开已有实例。端口被其他程序占用时提示错误，不会终止其他程序。Ctrl+C 停止本次服务；关闭网页后，终端中的进程仍可继续测试。

也保留 npm 安装入口，需要 Node.js 20+ 和 Python 3.10+。npm 注册表尚未发布，请使用 GitHub Release 提供的安装包：

```sh
npm install -g https://github.com/dahuaxiya/pelican-bench/releases/download/v0.5.0/pelican-bench-0.5.0.tgz
pelican-bench
```

Homebrew 直接调用 Python，npm 通过 Node 启动同一个 Python 后端。两种入口共用用户数据目录和实例锁，切换渠道不需要迁移成绩。请选择一种全局安装方式，避免两个包管理器争用同名命令。

## 服务命令

~~~sh
pelican-bench start                        # 前台启动并打开网页，与不带参数相同
pelican-bench start -d --no-browser        # 后台启动，关闭终端也继续运行
pelican-bench status                      # 查看是否运行、是否有在途任务
pelican-bench open performance            # 打开测速页；也支持 iq、settings、engineering
pelican-bench logs --lines 50
pelican-bench logs -f                     # 持续查看后台日志
pelican-bench stop                        # 停止空闲服务
pelican-bench stop --wait --timeout 600    # 等任务完成后停止，最多等 600 秒
pelican-bench restart -d --no-browser      # 重启空闲服务
~~~

stop 和 restart 只操作当前数据目录的实例。还有测评或复核时默认拒绝停止；可先用 cancel 停止指定批次。等待超时不会强杀进程。旧版已启动的服务需要先在原终端按 Ctrl+C，再用新命令启动。

--data-dir 可放在任意子命令后；自定义目录的启动、查询、停止要使用同一个目录。后台日志在该目录的 server.log。status 和 doctor 不启动服务；配置、查询结果或发起测试时，若没有运行实例，会后台启动一个。默认端口被其他程序占用时，先用 start -d --port 9000 指定空闲端口。

## 直接用 CLI 测试

第一次使用，可以在网页设置站点，也可以用命令配置：

~~~sh
pelican-bench sites list
# MODEL_API_KEY 是自己设置的环境变量名，命令不回显密钥。
pelican-bench sites set my-api --base-url https://example.com/v1 --api-key-env MODEL_API_KEY
pelican-bench models --site my-api
# 接口不提供 /models 时，显式配置候选模型，可重复传入 --model。
pelican-bench sites set my-api --model gpt-6.1-sol
~~~

也可用 --api-key-stdin 从标准输入接收密钥。修改单个站点保留其他配置；更换地址且未提供新密钥时清除旧密钥。内置 ChatGPT、Kimi 渠道请在设置页登录；CLI 使用 sites list 中的同一站点名。sites remove NAME 删除普通站点配置，不删除历史成绩。

~~~sh
# 单个模型测速，终端显示进度和结果
pelican-bench test performance --site my-api --model gpt-6.1-sol

# 同一站点全部模型并发测
pelican-bench test performance --site my-api --all-models --concurrency 32

# 多个站点、模型的明确组合
pelican-bench test performance --target my-api gpt-6.1-sol --target another-api other-model

# 容量检测
pelican-bench test capacity --site my-api --model gpt-6.1-sol

# IQ 默认先跑 23 题；正式测评选 bbeh-mini，需要先安装题库
pelican-bench test iq --site my-api --model gpt-6.1-sol --suite bbeh-smoke --concurrency 32

# SVG 使用网页保存的提示词和预算；工程评测需要先准备 Docker 和数据
pelican-bench test svg --site my-api --model gpt-6.1-sol
pelican-bench test engineering --site my-api --model gpt-6.1-sol --suite smoke
~~~

测试从当前电脑发出，可能使用模型额度。--site 和 --model 都可重复，组合测试；--target 指定精确组合，不能与它们混用。测速、容量和 IQ 并发可设为 1 到 200；默认沿用网页配置规则。

默认等待测试结束，IQ 还会等待原批次自动重试和自动复核。复核模型在网页设置；不可用或复核失败时显示未完成状态，不把临时分当最终分。--no-wait 提交后立即返回批次 ID；Ctrl+C 或 --wait-timeout 只停止 CLI 等待，不取消后台任务。

性能测速收到首个正文片段后，终端会立即显示该站点模型的首字延迟（TTFT），最终结果也保留这一列。在本站登录的 ChatGPT 账号通过 Codex app-server 采集；旧记录和没有正文增量的渠道显示 -，不拿整条回答耗时替代首字。JSON 的最终指标字段是 rows[].metrics.ttft_seconds，单位为秒，保留原始精度；缺失时为 null。

IQ 榜单同时展示同一次测评的复核前、复核后成绩与名次，可按任一成绩或平均耗时排序。每个站点模型保留最新有效成绩；新记录尚未复核完成时，继续展示已有最终成绩。

~~~sh
pelican-bench results performance                 # 历史批次列表（包含容量检测）
pelican-bench results performance RUN_ID --watch # 持续查看指定批次
pelican-bench results iq RUN_ID --full --json     # 完整题目与回答
pelican-bench cancel performance RUN_ID          # 也支持 capacity、iq、engineering
pelican-bench retry iq RUN_ID                    # 原地重试接口失败项
pelican-bench test performance --site my-api --model gpt-6.1-sol --json > result.json
pelican-bench test iq --help
~~~

SVG 目前不支持单独取消，等待完成后再 stop。网页与 CLI 共用历史；--json 的标准输出只包含 JSON，进度写到 stderr。退出码：0 为命令成功；1 为连接或操作失败；2 为参数错误；3 为测评、取消或复核未完整成功；4 为等待超时；130 为用户中断等待。模型答错、工程题未修复属于测评结果，不会仅因此返回非零码。

## VPN 节点测速

网页入口是「节点测速」，CLI 使用同一个本机后端和历史记录。当前支持 Clash/Mihomo 兼容控制口及本机 HTTP/Mixed 代理，需要可读取的 ChatGPT 登录态。控制地址和代理端口以自己客户端的配置为准。

```bash
pelican-bench open network
pelican-bench vpn config --controller http://127.0.0.1:9090 --proxy http://127.0.0.1:7890
pelican-bench vpn discover
pelican-bench vpn config --group '你的选择组'
pelican-bench vpn test --top 6 --repeats 5
pelican-bench vpn test --mode model --model gpt-6.1-sol --top 3 --repeats 3 --sort-by ttft
pelican-bench vpn results
pelican-bench vpn results <批次ID> --json
pelican-bench vpn apply
pelican-bench vpn apply <批次ID>
pelican-bench vpn cancel <批次ID>
pelican-bench vpn recover
```

控制口需要密钥时，用 `vpn config --secret-env CLASH_SECRET` 从环境变量读取；密钥只保存在本机私有配置，不出现在结果中。`vpn test --node '节点名'` 可重复传入，限定本次范围；`--no-wait` 提交后返回，使用 `vpn results <批次ID> --watch` 继续查看。

测试先对所选组的具体节点做 HTTPS 初筛，再对最快的候选及范围内原节点轮流测相同次数。`--top` 控制候选数，最高 50；设为 50 可测本次初筛通过的全部节点（不超过 50 个，另含原节点）。嵌套自动组不直接参与排名，请选包含实际节点的手动组。每次官方请求都核对实际连接经过的节点与选择组。

两种模式分别出排名：

- **握手测速**（默认）：只测认证 WebSocket 建连，不发送模型问题。排名依次比较成功率、握手中位延迟、最慢成功耗时；波动是成功延迟的极差。单次默认 12 秒，最高 60 秒。
- **模型实测**：需要先在本站设置中登录 ChatGPT，并选择账号提供的模型；使用订阅额度。各节点调用相同模型、相同提示词，要求生成 600–800 词，推理强度固定 medium，输出长度由模型决定。单次默认 180 秒，最高 600 秒。排名先看成功率，再按首字耗时中位数或端到端吞吐中位数排序；CLI 使用 `--sort-by ttft` 或 `--sort-by throughput`。

两种模式默认并发 6，网页可调整，CLI 使用 `--concurrency 6`，范围 1–50。每轮各节点各测一次，下一轮等待本轮结束。建立连接时依次切换并核对实际路由，连接固定后并发收取响应；模型内部重连也重新核对。握手延迟不含本地调度等待，模型首字和端到端耗时包含节点建连等待。并发会共享本机带宽和账号额度；要比较节点独占性能，设为 `--concurrency 1`。

首字耗时从本次官方客户端启动计到首个非空正文增量，包含启动、网络、排队和推理。端到端吞吐 = 上游实际输出 token 数（含推理）÷ 完整响应耗时，不代表首字之后的纯生成速度。缺少首字事件、缺少真实用量或回答过短时不参与速度排名，不用字符数估算 token。展开节点可看每次首字、吞吐、总耗时和用量；缓存、官方负载和客户端内部重连也会影响结果。

未测满的节点保留明细但没有最终名次；全失败没有延迟分数；只有本轮全成功且指标完整的候选才作为主用或备用建议。每次结果独立保存，不混排握手与模型实测，也不混排不同时段的数据。

测试会临时切换所选组，影响本机新建连接，因此与本服务的模型评测、复核及登录互斥。结束、取消、正常停机时恢复原节点并回读确认；强杀后在下次启动尝试恢复，失败可执行 `vpn recover`。检测到外部切换时停止并保留外部选择。Clash 没有原子比较交换接口，请勿同时运行其他节点切换程序。不会断开已有连接、修改订阅或规则，也不会自动长期切换到推荐节点。

点击排名旁的「切换最优节点」，会采用当前展示批次中全部测试成功且指标完整的第一名。`vpn apply` 默认使用当前配置最新完成的批次，也可指定批次 ID。切换后重新核对节点并验证官方握手，不额外生成文本；成功才保留新选择，失败恢复点击前的节点。历史排名不代表当前速度，网络变化后应重新测速。切换影响新连接，已有长连接继续使用原连接。

握手目标为 `chatgpt.com/backend-api/codex/responses`，成功要求 HTTP 101 和完整 WebSocket 升级校验；普通 HTTPS 成功不算。401、429、5xx 会停止握手测试整批，403 作为该节点的拒绝记录。模型实测遇到认证、额度、限流或容量错误会停止，避免把上游故障当成节点速度。少量样本不能证明长期稳定或所有模型都可用；模型实测结果也不能全部归因于 VPN。

## 数据与升级

| 系统 | 默认数据目录 |
| --- | --- |
| macOS | ~/Library/Application Support/pelican-bench |
| Linux | $XDG_DATA_HOME/pelican-bench，未设置时为 ~/.local/share/pelican-bench |

使用 --data-dir 或 PELICAN_BENCH_DATA_DIR 可以改目录。升级和卸载程序会保留此目录；其中含凭据，不要公开上传。

```sh
brew update
brew upgrade dahuaxiya/tap/pelican-bench
# 只卸载程序，保留个人数据
brew uninstall pelican-bench
```

服务只监听 127.0.0.1，保留 Host 与 Origin 检查。网页和后端都在本机，不经过项目的云端代理。配置远程模型时，推理仍由远程模型服务完成，测速包含用户的网络、代理和上游等待时间。

## 可选功能

| 功能 | 额外准备 |
| --- | --- |
| HTTP API 测速、容量检测、SVG | 自己的模型地址和凭据 |
| BBEH Mini、ARC-AGI-2 | 下载固定版本官方题库 |
| ChatGPT、Kimi 账号渠道 | 安装对应官方 CLI，并登录自己的账号 |
| 工程测评 | Docker、额外 Python 依赖与 SWE-bench 数据 |

先停止该数据目录的服务，再按需准备题库或工程环境：

```sh
pelican-bench setup-iq bbeh
pelican-bench setup-iq arc
pelican-bench setup-engineering
```

准备题库和工程环境会下载公开文件，不会发起模型推理。固定版本、题库原文及原许可证会保存在本地。也可以使用 --archive 或 --parquet 指定预先下载的文件。

工程评测默认使用 Docker 的 default context；其他环境通过 PELICAN_BENCH_DOCKER_CONTEXT 指定。doctor 只检查依赖是否存在，不代表账号或模型请求已经验证成功。

Homebrew 渠道面向 macOS 和 Linux，Windows 可在 WSL2 内使用 Linux 环境。当前发布流程在 macOS Apple Silicon 验证；其他平台的运行结果以 CI 和实际测试为准。

## 源码与许可

基础运行只需要 Python 3.10+ 的标准库：

```sh
python3 bin/pelican.py
python3 -m unittest discover -s tests -v
```

项目采用 MIT 许可证。Lucide 的许可证保存在 static/vendor/lucide.LICENSE。公开源码不包含开发者的站点配置、密钥、登录态、成绩或日志。
