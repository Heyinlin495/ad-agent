#!/usr/bin/env node
/**
 * 零构建 ESM 的「链接期检查」
 *
 * 项目没有打包器，`import { x } from "./y.js"` 写错名字不会在构建阶段报错，
 * 只会在运行时报 SyntaxError 甚至静默拿到 undefined。本脚本遍历所有模块，
 * 校验：① import 路径存在；② 每个具名导入都能在目标模块的导出中找到；
 * ③ 不发生大小写/扩展名写错的路径问题。
 *
 * 用法：node tools/check.mjs   （在 web/ 目录下执行，发现问题退出码为 1）
 */
import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const ENTRY = join(WEB_DIR, "js", "main.js");

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (name.endsWith(".js")) out.push(full);
  }
  return out;
}

/** 收集模块的具名导出 */
function collectExports(code) {
  const names = new Set();
  const patterns = [
    /\bexport\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/g,
    /\bexport\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g,
    /\bexport\s+class\s+([A-Za-z_$][\w$]*)/g,
    /\bexport\s*\{([^}]*)\}/g,
  ];
  for (const re of patterns) {
    let m;
    while ((m = re.exec(code)) !== null) {
      if (re.source.includes("\\{")) {
        for (const part of m[1].split(",")) {
          const name = part.trim().split(/\s+as\s+/).pop().trim();
          if (name) names.add(name);
        }
      } else {
        names.add(m[1]);
      }
    }
  }
  if (/\bexport\s+default\b/.test(code)) names.add("default");
  return names;
}

/** 收集 import 语句：{ specifiers, from } */
function collectImports(code) {
  const out = [];
  const re = /import\s+([^;'"]*?)\s*from\s*["']([^"']+)["']/g;
  let m;
  while ((m = re.exec(code)) !== null) {
    const clause = m[1].trim();
    const source = m[2].split("?")[0];
    const specifiers = [];
    const braced = clause.match(/\{([^}]*)\}/);
    if (braced) {
      for (const part of braced[1].split(",")) {
        const t = part.trim();
        if (t) specifiers.push(t.split(/\s+as\s+/)[0].trim());
      }
    }
    const def = clause.replace(/\{[^}]*\}/, "").replace(/,/g, " ").trim();
    if (def) specifiers.push("default");
    out.push({ specifiers, source });
  }
  return out;
}

const files = walk(join(WEB_DIR, "js"));
const modules = new Map(files.map((f) => [f, readFileSync(f, "utf8")]));
const problems = [];
const rel = (p) => p.replace(WEB_DIR, "web").replace(/\\/g, "/");

// 1) 入口可达性
if (!existsSync(ENTRY)) problems.push(`入口不存在：${ENTRY}`);

// 2) 逐模块校验
const reachable = new Set();
const queue = [ENTRY];
while (queue.length) {
  const file = queue.pop();
  if (reachable.has(file) || !modules.has(file)) continue;
  reachable.add(file);

  for (const { specifiers, source } of collectImports(modules.get(file))) {
    if (!source.startsWith(".")) continue; // 裸模块说明符
    const target = resolve(dirname(file), source);
    if (!existsSync(target)) {
      problems.push(`${rel(file)} → 找不到模块 ${source}`);
      continue;
    }
    const exportsOf = collectExports(modules.get(target) || readFileSync(target, "utf8"));
    for (const name of specifiers) {
      if (!exportsOf.has(name)) {
        problems.push(`${rel(file)} → 从 ${source} 导入的 \`${name}\` 未导出`);
      }
    }
    queue.push(target);
  }
}

const orphans = files.filter((f) => !reachable.has(f));

console.log(`[check] 模块 ${files.length} 个，从入口可达 ${reachable.size} 个`);
if (orphans.length) console.log(`[check] 未被引用的模块：${orphans.map(rel).join(", ")}`);

if (problems.length) {
  console.log(`\n[check] 发现 ${problems.length} 处问题：`);
  for (const p of problems) console.log(`  ✗ ${p}`);
  process.exit(1);
}
console.log("[check] 导入/导出一致性 OK");
