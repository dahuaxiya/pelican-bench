/** 发布前检查只允许具体资源文件；不扫描或输出开发者的配置与凭据。 */
import { lstatSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const pkg = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'));
const forbidden = /(^|\/)(?:config\.json(?:\..*)?|\.[^/]+|node_modules|runs|output|logs|iq-data|iq-runs|iq-reviews|swe-data|swe-runs)(\/|$)/;
for (const entry of pkg.files) {
  if (entry.includes('..') || /[*!?]/.test(entry) || path.isAbsolute(entry) || forbidden.test(entry)) {
    throw new Error(`Unsafe package entry: ${entry}`);
  }
  const stat = lstatSync(path.join(root, entry));
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`Package entry must be a regular file: ${entry}`);
}
// stderr 保留诊断，避免污染 npm pack --json 给安装验证器读取的标准输出。
console.error(`Package allowlist checked: ${pkg.files.length} runtime files. No local state directories.`);
