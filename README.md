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
npm install -g ./pelican-bench-0.3.0.tgz
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
