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
npm install -g ./pelican-bench-0.2.0.tgz
pelican-bench
```

Homebrew 直接调用 Python，npm 通过 Node 启动同一个 Python 后端。两种入口共用用户数据目录和实例锁，切换渠道不需要迁移成绩。请选择一种全局安装方式，避免两个包管理器争用同名命令。

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
