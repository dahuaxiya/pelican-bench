#!/usr/bin/env node
/** npm 入口只发现本机 Python 并传参；不安装运行时、不读取密钥、不走云端代理。 */
import { spawn, spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);

/** 取出启动器选项的单个值；不拆分为 shell 命令，支持含空格的可执行文件路径。 */
function option(name) {
  const index = args.indexOf(name);
  if (index < 0) return undefined;
  if (!args[index + 1] || args[index + 1].startsWith('--')) throw new Error(`${name} requires a value`);
  return args.splice(index, 2)[1];
}

/** 返回跨升级保留的每用户数据目录，不依赖 npx 缓存或当前工作目录。 */
function dataDirectory() {
  if (process.platform === 'win32') return path.join(process.env.LOCALAPPDATA || path.join(homedir(), 'AppData', 'Local'), 'pelican-bench');
  if (process.platform === 'darwin') return path.join(homedir(), 'Library', 'Application Support', 'pelican-bench');
  return path.join(process.env.XDG_DATA_HOME || path.join(homedir(), '.local', 'share'), 'pelican-bench');
}

/** 要求 Python 3.10+；显式指定失败时直接报错，不偷偷换用别的解释器。 */
function pythonCommand(explicit) {
  const candidates = explicit ? [[explicit]] : process.platform === 'win32'
    ? [['py', '-3'], ['python'], ['python3']] : [['python3'], ['python']];
  for (const candidate of candidates) {
    const check = spawnSync(candidate[0], [...candidate.slice(1), '-I', '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'],
      { stdio: 'ignore', timeout: 10000, windowsHide: true, shell: false });
    if (!check.error && check.status === 0) return candidate;
  }
  throw new Error('Python 3.10+ is required. Install Python, or use --python <executable-path>.');
}

/** 将退出与信号交给本次子进程；不查找、不终止机器上其他测评实例。 */
function main() {
  if (args.includes('--version') || args.includes('-v')) {
    console.log(JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8')).version);
    return;
  }
  if (args.includes('--help') || args.includes('-h')) {
    console.log(`Pelican Bench - local model benchmarks

Usage: pelican-bench [start|doctor|setup-iq|setup-engineering] [options]
  start                       Open the local web UI (default)
  doctor [--json]             Check local dependencies without calling models
  setup-iq bbeh|arc            Download a pinned official question set
  setup-engineering           Install optional Python environment and SWE data
  --data-dir <path>            Keep config, accounts and results in this directory
  --python <path>              Select Python 3.10+ (3.11+ for engineering)
  --port <number>              Local port, default 8780; 0 selects a free port
  --no-browser                Do not open a browser
  --archive <path>             Use a local official archive with setup-iq
  --parquet <path>             Use local SWE data with setup-engineering

Requires Node.js 20+ and Python 3.10+. Model requests run on this computer.
Set PELICAN_BENCH_DOCKER_CONTEXT to select an engineering Docker context.`);
    return;
  }
  const explicitPython = option('--python') || process.env.PELICAN_BENCH_PYTHON;
  const data = path.resolve(option('--data-dir') || process.env.PELICAN_BENCH_DATA_DIR || dataDirectory());
  const python = pythonCommand(explicitPython);
  const child = spawn(python[0], [...python.slice(1), '-u', path.join(root, 'local_launcher.py'), ...args], {
    cwd: process.cwd(), stdio: 'inherit', shell: false,
    env: { ...process.env, PELICAN_BENCH_DATA_DIR: data, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' },
  });
  // 终端关闭或 npm 转发信号时让 Python 释放本实例锁；不使用 pkill 等全局查杀。
  const interrupt = () => child.kill('SIGINT');
  const terminate = () => child.kill('SIGTERM');
  process.on('SIGINT', interrupt);
  process.on('SIGTERM', terminate);
  child.once('error', error => { console.error(error.message); process.exitCode = 1; });
  child.once('exit', (code, signal) => {
    process.removeListener('SIGINT', interrupt);
    process.removeListener('SIGTERM', terminate);
    process.exitCode = code ?? (signal ? 1 : 0);
  });
}

try { main(); } catch (error) { console.error(error.message); process.exitCode = 1; }
