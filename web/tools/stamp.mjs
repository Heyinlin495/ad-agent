#!/usr/bin/env node
/**
 * 资源版本戳工具
 *
 * 为什么需要：前端是「零构建」的原生 ESM，浏览器对 js/css 的缓存策略依赖
 * 查询串。此前靠手工维护 `?v=20261007a`，改完忘记 bump 就会拿到旧代码。
 *
 * 做法：
 *   1. 读取 js/ 与 css/ 下所有源文件，去掉已存在的 `?v=xxx` 后计算内容哈希；
 *   2. 把同一个哈希写回 index.html 的 <link>/<script> 引用；
 *   3. 把同一个哈希追加到所有相对 import 说明符上（ESM 子模块也能正确失效）。
 *
 * 用法：node tools/stamp.mjs        （在 web/ 目录下执行）
 */
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SRC_DIRS = ["js", "css"];
const VERSION_RE = /\?v=[0-9a-z]+/g;

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (/\.(js|css)$/.test(name)) out.push(full);
  }
  return out;
}

/** 去掉既有的版本查询串后再哈希，保证多次执行结果稳定（幂等） */
const stripVersion = (text) => text.replace(VERSION_RE, "");

const files = SRC_DIRS.flatMap((d) => walk(join(WEB_DIR, d)));
const hash = createHash("sha1");
for (const f of files.sort()) hash.update(stripVersion(readFileSync(f, "utf8")));
const version = hash.digest("hex").slice(0, 8);

let touched = 0;

// 1) js 内部的相对 import 说明符
for (const f of files.filter((f) => f.endsWith(".js"))) {
  const original = readFileSync(f, "utf8");
  const next = original.replace(
    /(\bfrom\s+["'])(\.{1,2}\/[^"']+?\.js)(\?v=[0-9a-z]+)?(["'])/g,
    (_m, head, spec, _old, tail) => `${head}${spec}?v=${version}${tail}`
  );
  if (next !== original) {
    writeFileSync(f, next, "utf8");
    touched += 1;
  }
}

// 2) index.html 的入口引用
const htmlPath = join(WEB_DIR, "index.html");
const html = readFileSync(htmlPath, "utf8");
const nextHtml = html.replace(
  /((?:src|href)=")((?:js|css)\/[^"?]+)(\?v=[0-9a-z]+)?(")/g,
  (_m, head, url, _old, tail) => `${head}${url}?v=${version}${tail}`
);
if (nextHtml !== html) {
  writeFileSync(htmlPath, nextHtml, "utf8");
  touched += 1;
}

console.log(
  `[stamp] version = ${version}  |  扫描 ${files.length} 个源文件，更新 ${touched} 个文件引用`
);
console.log(`[stamp] 提示：改动前端后执行一次即可，无需再手工 bump 版本号。`);
