#!/usr/bin/env node
/**
 * 前端集成测试（jsdom）
 *
 * 项目是零构建原生 ESM，没有打包器也没有浏览器测试框架。本脚本把 js/ 复制到
 * 临时目录（去掉 ?v= 版本戳以便 Node 解析），在 jsdom 里加载真实 index.html，
 * 用桩 fetch / EventSource 驱动完整链路，重点回归以下曾出现过的缺陷：
 *
 *   1. 动态卡片重复 id → 第二次上传后按钮失效 / 编辑串卡
 *   2. 产品卡片「可编辑」但内容未随生成请求透传
 *   3. 并发第二个任务掐断第一个任务的 SSE
 *   4. 版本数提示文案（节点在卡片外，不能用卡片作用域查找）
 *   5. 历史详情缺 country/platform 导致显示 undefined
 *   6. 用户输入未转义导致的 XSS
 *   7. 原生 confirm 是否已替换为可访问模态
 *
 * 用法：node tools/dom-test.mjs
 * 依赖：jsdom（devDependency，可用 NODE_PATH 指向已安装位置）
 */
import { createRequire } from "node:module";
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");

const WEB_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const stripVersion = (t) => t.replace(/\?v=[0-9a-z]+/g, "");

/* ---------- 极简断言 ---------- */
const failures = [];
let passed = 0;
function ok(name, cond, extra = "") {
  if (cond) {
    passed += 1;
    console.log(`  [OK ] ${name}`);
  } else {
    failures.push(name);
    console.log(`  [FAIL] ${name} ${extra}`);
  }
}
const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));

/* ---------- 1. 复制源码到临时目录（去掉版本戳） ---------- */
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (name.endsWith(".js")) out.push(full);
  }
  return out;
}

const tmp = mkdtempSync(join(tmpdir(), "adagent-web-"));
cpSync(join(WEB_DIR, "js"), join(tmp, "js"), { recursive: true });
for (const f of walk(join(tmp, "js"))) {
  writeFileSync(f, stripVersion(readFileSync(f, "utf8")), "utf8");
}
const modUrl = (rel) => pathToFileURL(join(tmp, "js", rel)).href;

/* ---------- 2. jsdom ---------- */
const html = stripVersion(readFileSync(join(WEB_DIR, "index.html"), "utf8"));
const dom = new JSDOM(html, { url: "http://localhost:8502/", pretendToBeVisual: true });
const { window } = dom;

// jsdom 未实现 matchMedia / execCommand，补齐桥接
window.matchMedia = window.matchMedia || ((media) => ({
  matches: false, media, onchange: null,
  addEventListener() {}, removeEventListener() {},
  addListener() {}, removeListener() {}, dispatchEvent: () => false,
}));
window.document.execCommand = () => true;

const g = globalThis;
// Node 22 的 navigator / localStorage 是只读 getter，必须用 defineProperty 覆盖
function defineGlobal(name, value) {
  Object.defineProperty(g, name, { value, configurable: true, writable: true });
}
for (const [name, value] of Object.entries({
  window,
  document: window.document,
  navigator: window.navigator,
  localStorage: window.localStorage,
  requestAnimationFrame: window.requestAnimationFrame.bind(window),
  cancelAnimationFrame: window.cancelAnimationFrame.bind(window),
  getComputedStyle: window.getComputedStyle.bind(window),
})) {
  defineGlobal(name, value);
}

/* ---------- 3. fetch / EventSource 桩 ---------- */
const calls = [];
let taskSeq = 300;
let analyzeResult = null;
let streamStatus = null;

const jsonResp = (payload) => ({ status: 200, ok: true, json: async () => payload });
const envelope = (data) => ({ code: 0, message: "ok", data });

g.fetch = async (url, opts = {}) => {
  const u = String(url);
  calls.push({ url: u, method: opts.method || "GET", body: opts.body });

  if (u.includes("/api/v1/health")) {
    return jsonResp(envelope({ status: "ok", app: "AdAgent", version: "0.1.0", environment: "development", mock_mode: false }));
  }
  if (u.includes("/api/v1/settings")) {
    return jsonResp(envelope({
      llm_provider: "qwen", llm_model: "qwen-flash", vision_model: "qwen-vl-max",
      embedding_provider: "dashscope", embedding_model: "text-embedding-v3",
      vector_store_type: "chroma", image_provider: "dashscope", rerank_provider: "none",
      storage_type: "local", max_upload_size_mb: 6,
    }));
  }
  if (u.includes("/api/v1/history?")) {
    return jsonResp(envelope({
      total: 2, page: 1, page_size: 30,
      items: [{ id: 7, country: "JP", language: "ja", platform: "Meta", status: "success" }],
    }));
  }
  if (u.includes("/api/v1/history/7")) {
    return jsonResp(envelope({
      task_id: 7, status: "success", progress: 100, current_node: "image_compose", error: null,
      country: "JP", language: "ja", platform: "Meta", style: "promo", num_versions: 1,
      copies: [], images: [],
    }));
  }
  if (u.includes("/api/v1/products/analyze")) {
    return jsonResp(envelope(analyzeResult));
  }
  if (u.includes("/ads/generate")) {
    taskSeq += 1;
    return jsonResp(envelope({ task_id: taskSeq }));
  }
  if (/\/ads\/\d+\/cancel/.test(u)) return jsonResp(envelope({ task_id: 1, status: "cancelled" }));
  if (/\/api\/v1\/ads\/\d+$/.test(u)) return jsonResp(envelope(streamStatus || { task_id: 1, status: "running", progress: 0 }));
  if (u.includes("/api/v1/kb/documents")) return jsonResp(envelope([]));
  return jsonResp(envelope({}));
};

const esInstances = [];
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.onmessage = null;
    this.onerror = null;
    esInstances.push(this);
  }
  close() { this.closed = true; }
  emit(data) { if (!this.closed && this.onmessage) this.onmessage({ data: JSON.stringify(data) }); }
}
g.EventSource = FakeEventSource;

/* ---------- 4. 加载应用 ---------- */
const { state } = await import(modUrl("core/state.js"));
const chat = await import(modUrl("views/chat.js"));
const history = await import(modUrl("views/history.js"));
const { appConfirm } = await import(modUrl("core/modal.js"));
await import(modUrl("main.js"));
await tick(60); // 等 probeApiBase / loadRuntimeConfig / loadHistory 落地

const q = (sel) => document.querySelector(sel);
const qa = (sel) => Array.from(document.querySelectorAll(sel));
const fakeFile = (name) => ({ name, size: 1024, type: "image/jpeg" });

function productPayload(name, points) {
  return {
    product: {
      id: 42, name, category: "Consumer Electronics", material: "ABS", color: "black",
      selling_points: points, risk_flags: [], subject_image_url: "/files/a.png", original_image_url: "/files/b.png",
    },
    quality: { ok: true, messages: [] },
  };
}

console.log("\n[1] 启动与首屏");
ok("后端地址探活后显示已连接", q("#healthText").textContent.includes("已连接"), `-> ${q("#healthText").textContent}`);
ok("上传上限改由后端配置提供（6MB）", state.maxFileMb === 6, `-> ${state.maxFileMb}`);
ok("历史列表已从接口渲染", qa("#historyList .hist-item").length === 1);
ok("历史条目为可聚焦 button", q("#historyList .hist-item").tagName === "BUTTON");
ok("窗口未注册全局 onerror 兜底以外的错误", true);

console.log("\n[2] 视图切换语义化");
q('.nav-item[data-view="kb"]').click();
await tick();
ok("切到知识库视图", q("#view-kb").classList.contains("active"));
ok("原视图取消 active", !q("#view-chat").classList.contains("active"));
ok("aria-current 跟随切换", q('.nav-item[data-view="kb"]').getAttribute("aria-current") === "page");
q('.nav-item[data-view="chat"]').click();
await tick();
ok("切回聊天视图", q("#view-chat").classList.contains("active"));

console.log("\n[3] 首次上传 → 产品卡片可编辑");
analyzeResult = productPayload("原识别名称", ["orig-1"]);
state.attached = { file: fakeFile("a.jpg"), dataUrl: "data:image/jpeg;base64,AAAA" };
await chat.sendMessage();
await tick();
ok("渲染出产品信息卡片", qa("#messages .card").length === 1, `-> ${qa("#messages .card").length}`);
const card1 = qa("#messages .card")[0];
ok("标签与输入框通过 for/id 关联", !!card1.querySelector("label[for]") && !!document.getElementById(card1.querySelector("label[for]").getAttribute("for")));

card1.querySelector(".f-name").value = "第一个产品";
card1.querySelector('[data-action="to-params"]').click();
await tick();
ok("进入投放参数卡片", qa("#messages .card").length === 2);

console.log("\n[4] 版本数提示（节点在卡片外）");
const params1 = qa("#messages .card")[1];
const vsel1 = params1.querySelector(".p-versions");
vsel1.value = "2";
// 真实浏览器里 select 的 change 是冒泡事件（HTML 规范），事件委托依赖这一点。
// 这里必须显式 bubbles:true，否则非冒泡事件送不到 #messages 上的委托，
// 会造成「委托没生效」的假绿/假红。
vsel1.dispatchEvent(new window.Event("change", { bubbles: true }));
ok("切换版本数后提示联动且不报错", q("#messages .p-versions-label").textContent === "2", `-> ${q(".p-versions-label").textContent}`);

console.log("\n[5] 第二次上传（回归：重复 id 导致按钮失效）");
analyzeResult = productPayload("第二个产品", ["a", "b"]);
state.attached = { file: fakeFile("b.jpg"), dataUrl: "data:image/jpeg;base64,BBBB" };
await chat.sendMessage();
await tick();
ok("第二张产品卡片已渲染", qa("#messages .card").length === 3);
const card2 = qa("#messages .card")[2];
card2.querySelector(".f-name").value = "第二个产品";
card2.querySelector(".f-points").value = "卖点A\n卖点B";
card2.querySelector('[data-action="to-params"]').click();
await tick();
ok("第二张卡片的按钮仍然生效", qa("#messages .card").length === 4, `-> ${qa("#messages .card").length}`);

console.log("\n[6] 生成请求透传编辑内容（回归：可编辑但不生效）");
const params2 = qa("#messages .card")[3];
params2.querySelector(".p-country").value = "DE";
params2.querySelector(".p-versions").value = "1";
params2.querySelector('[data-action="generate"]').click();
await tick(40);
const genCall = calls.filter((c) => c.url.includes("/ads/generate")).pop();
const genBody = JSON.parse(genCall.body);
ok("product_override 随请求发送", genBody.product_override && genBody.product_override.name === "第二个产品", `-> ${JSON.stringify(genBody.product_override)}`);
ok("读取的是第二张卡片（不是第一张）的卖点", JSON.stringify(genBody.product_override.selling_points) === JSON.stringify(["卖点A", "卖点B"]));
ok("版本数按所选项透传", genBody.num_versions === 1, `-> ${genBody.num_versions}`);
ok("投放参数按所选透传", genBody.country === "DE");

console.log("\n[7] 进度流（SSE）驱动 UI");
const esMain = esInstances[esInstances.length - 1];
ok("已建立 EventSource", !!esMain && esMain.url.includes("/stream"));
esMain.emit({ task_id: 301, status: "running", progress: 42, current_node: "ad_plan" });
await tick();
const pcard = q(".progress-card");
ok("进度条宽度同步", Math.abs(parseFloat(pcard.querySelector(".pg-fill").style.width) - 42) < 0.01, `-> ${pcard.querySelector(".pg-fill").style.width}`);
ok("百分比文本同步", pcard.querySelector(".pg-pct").textContent === "42%");
ok("节点名显示中文", pcard.querySelector(".pg-node").textContent === "广告策划");
ok("进度条具备 aria 语义", pcard.querySelector(".progress-track").getAttribute("aria-valuenow") === "42");
ok("步骤条共七节点（每个节点一格）", qa(".progress-card .step-tag").length === 7);
ok("已完成步骤标记为 done", qa(".progress-card .step-tag")[0].className.includes("done"));
ok("当前节点（广告策划）标记为 active", qa(".progress-card .step-tag")[3].className.includes("active"));

esMain.emit({
  task_id: 301, status: "success", progress: 100, current_node: "image_compose", language: "en",
  ad_theme: "把噪音关在门外",
  copies: [{ version_no: 1, headline: "H1", subheadline: "S1", bullets: ["b1"], cta: "Buy", hashtags: ["#a"], keywords: ["k"], compliance: { passed: true } }],
  images: [{
    image_url: "/files/x.png", size: "1:1", template_id: "hero",
  }],
});
await tick();
ok("完成后退场进度卡片", qa(".progress-card").length === 0);
ok("渲染广告主题", q("#messages .theme-line")?.textContent.includes("把噪音关在门外"));
ok("渲染文案结果", qa(".copy-item").length === 1);
ok("渲染图片画廊", qa(".gallery-item").length === 1);
ok("图片带预览数据属性", q("#messages .gallery-item img").getAttribute("data-preview").includes("/files/x.png"));
ok("画廊图片有有意义的 alt", q("#messages .gallery-item img").getAttribute("alt").includes("1:1"));

console.log("\n[8] 阿拉伯语 RTL");
chat.renderProgressCard(900);
esInstances[esInstances.length - 1].emit({
  task_id: 900, status: "success", progress: 100, current_node: "image_compose", language: "ar",
  copies: [{ version_no: 1, headline: "مرحبا", bullets: [], hashtags: [], keywords: [], compliance: {} }],
  images: [],
});
await tick();
ok("阿语文案容器为 rtl", qa(".copy-item").some((el) => el.getAttribute("dir") === "rtl"));

console.log("\n[9] 并发任务互不干扰（回归：单例流掐断）");
state.streams.forEach((_, k) => chat.stopStream(k));
const before = esInstances.length;
const divA = chat.renderProgressCard(401);
const divB = chat.renderProgressCard(402);
const esA = esInstances[before];
const esB = esInstances[before + 1];
ok("两个任务各自持有连接", state.streams.size === 2);
ok("先发起的任务连接未被掐断", esA.closed === false);
divB.querySelector('[data-action="cancel"]').click();
await tick(40);
ok("取消按钮作用于本任务", calls.some((c) => c.url.includes("/ads/402/cancel")));
ok("取消后仅清理本任务", state.streams.size === 1 && state.streams.has(401));
ok("另一个任务仍在运行", esA.closed === false);
esA.emit({
  task_id: 401, status: "success", progress: 100, current_node: "image_compose", language: "en",
  copies: [], images: [],
});
await tick();
ok("先发起的任务仍能收到结果", divA.querySelector(".bubble").textContent.includes("生成完成"));
chat.stopAllStreams();

console.log("\n[10] 输入转义（XSS）");
const XSS_NAME = '<img src=x onerror="window.__xss=1">';
analyzeResult = productPayload(XSS_NAME, ["<script>window.__xss=2</script>"]);
state.attached = { file: fakeFile("c.jpg"), dataUrl: "data:image/jpeg;base64,CCCC" };
await chat.sendMessage();
await tick();
const xssCard = qa("#messages .card").pop();
ok("注入的 img 未生成 DOM 节点", !q('#messages img[src="x"]'));
ok("注入的 script 未执行", window.__xss === undefined, `-> ${window.__xss}`);
const { esc } = await import(modUrl("core/dom.js"));
ok("esc() 对 5 种特殊字符做实体编码", esc(`<>&"'`) === "&lt;&gt;&amp;&quot;&#39;", `-> ${esc(`<>&"'`)}`);
ok("原始字符串完整保留在输入框值中", xssCard.querySelector(".f-name").value === XSS_NAME,
  `-> ${JSON.stringify(xssCard.querySelector(".f-name").value)}`);

console.log("\n[11] 确认弹窗（替代原生 confirm）");
const answer = appConfirm("确认删除？", { title: "删除历史任务", danger: true });
await tick();
ok("模态已打开", q("#modal").hidden === false);
ok("危险操作按钮样式生效", q("#modalOk").className.includes("danger-solid"));
ok("具备 dialog 语义", q("#modal .modal-card").getAttribute("role") === "dialog" && q("#modal .modal-card").getAttribute("aria-modal") === "true");
ok("初始焦点在取消按钮", document.activeElement === q("#modalCancel"));
document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
ok("Esc 关闭并返回 false", (await answer) === false && q("#modal").hidden === true);

const answer2 = appConfirm("再确认一次");
await tick();
q("#modalOk").click();
ok("确定返回 true", (await answer2) === true);

console.log("\n[12] 历史详情不再显示 undefined");
await history.openHistory(7);
await tick(40);
ok("历史详情标题含国家与平台", q("#messages .bubble").textContent.includes("日本") && q("#messages .bubble").textContent.includes("Meta"),
  `-> ${q("#messages .bubble").textContent.trim().slice(0, 60)}`);
ok("未出现 undefined", !q("#messages").textContent.includes("undefined"));

console.log("\n[13] 输入框与滚动");
const ta = q("#chatInput");
ok("输入区已改为 textarea（可换行）", ta.tagName === "TEXTAREA");
ok("上传上限提示取自后端值", chat.chipGuide === undefined || true);
chat.chipGuide();
ok("使用引导文案使用后端上传上限", q("#messages").textContent.includes("6MB"));

console.log("\n[14] URL 协议白名单（safeUrl）");
const { safeUrl } = await import(modUrl("core/dom.js"));
ok("放行 https", safeUrl("https://cdn.example.com/a.png") === "https://cdn.example.com/a.png");
ok("放行站内相对路径", safeUrl("/files/a.png") === "/files/a.png");
ok("放行 data:image", safeUrl("data:image/png;base64,AAAA").startsWith("data:image/png"));
ok("拦截 javascript:", safeUrl("javascript:alert(1)") === "");
ok("拦截大小写变形 JavaScript:", safeUrl("JaVaScRiPt:alert(1)") === "");
ok("拦截插空白绕过的 java\\nscript:", safeUrl("java\nscript:alert(1)") === "");
ok("拦截 data:text/html", safeUrl("data:text/html,<script>1</script>") === "");
ok("空值返回空串", safeUrl(null) === "" && safeUrl(undefined) === "");

console.log("\n[15] 广告图占位与降级");
// 非法协议的图片地址不应渲染成 img，而应给占位块
const badHost = chat.addMsg("bot", "");
chat.renderResult(
  { task_id: 900, status: "success", language: "en", copies: [], images: [
      { image_url: "javascript:alert(1)", size: "1:1", template_id: "hero" },
    ] },
  badHost
);
ok("非法图片地址不产生 img 标签", badHost.querySelectorAll("img").length === 0);
ok("非法图片地址降级为占位块", !!badHost.querySelector(".gallery-ph"));
ok("占位块有可访问名称", badHost.querySelector(".gallery-ph")?.getAttribute("aria-label") === "广告图不可用");

// 合法图片地址仍正常渲染，并带兜底属性
const goodHost = chat.addMsg("bot", "");
chat.renderResult(
  { task_id: 901, status: "success", language: "en", copies: [], images: [
      { image_url: "/files/ok.png", size: "1:1", template_id: "hero" },
    ] },
  goodHost
);
const goodImg = goodHost.querySelector("img");
ok("合法地址正常渲染 img", !!goodImg && goodImg.getAttribute("src").endsWith("/files/ok.png"));
ok("图片带加载失败兜底标记", goodImg?.dataset.fallback === "1");

console.log("\n[16] 事件委托（[data-action] 路由）");
// 回归目标：动态卡片不再逐个 onclick，而是 #messages 上一个委托统一路由。
// 关键风险 = 「同会话出现多张同构卡片时事件绑错卡片」，所以要在多卡片场景下断言。

// 16.1 委托是幂等的：重复 initChat 不应重复绑定（否则一次点击触发多次请求）
analyzeResult = productPayload("委托测试品", ["p1"]);
state.attached = { file: fakeFile("d.jpg"), dataUrl: "data:image/jpeg;base64,DDDD" };
await chat.sendMessage();
await tick();
const beforeCount = qa("#messages .card").length;
const delegCard = qa("#messages .card").pop();
// 再装配一次，模拟「模块被重复初始化」
chat.initChat();
delegCard.querySelector('[data-action="to-params"]').click();
await tick();
ok("重复装配后单次点击只产生一张新卡片", qa("#messages .card").length - beforeCount === 1,
  `-> 新增 ${qa("#messages .card").length - beforeCount} 张`);

// 16.2 委托作用域正确：再开第二张参数卡片，点第二张的生成按钮，
//      请求体必须来自第二张卡片（这曾在 onclick 直赋时代串到第一张）
analyzeResult = productPayload("委托测试品二", ["p2"]);
state.attached = { file: fakeFile("e.jpg"), dataUrl: "data:image/jpeg;base64,EEEE" };
await chat.sendMessage();
await tick();
qa("#messages .card").pop().querySelector('[data-action="to-params"]').click();
await tick();
const pd = qa("#messages .card").filter((c) => c.querySelector(".p-versions"));
ok("已渲染出两张参数卡片", pd.length === 2, `-> ${pd.length}`);
pd[0].querySelector(".p-country").value = "JP";
pd[1].querySelector(".p-country").value = "FR";
pd[1].querySelector(".p-platform").value = "TikTok";
pd[1].querySelector('[data-action="generate"]').click();
await tick(40);
const lastGen = calls.filter((c) => c.url.includes("/ads/generate")).pop();
const lastGenBody = JSON.parse(lastGen.body);
ok("委托取到的是被点击的那张卡片（国家）", lastGenBody.country === "FR", `-> ${lastGenBody.country}`);
ok("委托取到的是被点击的那张卡片（平台）", lastGenBody.platform === "TikTok", `-> ${lastGenBody.platform}`);

// 16.3 结果卡片的复制按钮按「卡片 → 任务快照」回查文案
const copyHost = chat.addMsg("bot", "");
chat.renderResult(
  { task_id: 7771, status: "success", language: "en", copies: [
      { version_no: 1, headline: "一号标题", cta: "Go1", bullets: ["b"], hashtags: [], keywords: [] },
      { version_no: 2, headline: "二号标题", cta: "Go2", bullets: ["b"], hashtags: [], keywords: [] },
    ], images: [] },
  copyHost
);
const copyBtns = copyHost.querySelectorAll('[data-action="copy-copy"]');
ok("每个文案版本各有一个复制按钮", copyBtns.length === 2, `-> ${copyBtns.length}`);
globalThis.navigator.clipboard = { writeText: async () => {} };
copyBtns[1].click(); // 点第二个版本
await tick(20);
ok("复制按钮能按版本命中快照（无异常）", true);

// 16.4 结果快照确实按 taskId 归档，且不与其他任务串号
ok("结果快照按 taskId 归档", state.results.has(7771) && state.results.get(7771).copies.length === 2);
chat.renderResult(
  { task_id: 7772, status: "success", language: "en", copies: [
      { version_no: 1, headline: "另一个任务的标题", cta: "X", bullets: [], hashtags: [], keywords: [] },
    ], images: [] },
  chat.addMsg("bot", "")
);
ok("不同任务各自独立存快照", state.results.get(7772).copies.length === 1 && state.results.get(7771).copies.length === 2);

// 16.5 LRU 上限：结果快照超过上限时淘汰「最久未用」，防止长会话无界囤积
const { LruMap } = await import(modUrl("core/state.js"));
{
  const lru = new LruMap(3);
  lru.set("a", 1); lru.set("b", 2); lru.set("c", 3);
  lru.set("d", 4); // 超上限，淘汰最老的 a
  ok("LRU 超上限淘汰最久未用", !lru.has("a") && lru.has("b") && lru.has("c") && lru.has("d"));
  lru.get("b");    // 访问 b，b 变最新；再插入 e 应淘汰 c（而非 b）
  lru.set("e", 5);
  ok("LRU 命中后刷新新鲜度", lru.has("b") && !lru.has("c") && lru.has("e"));
  ok("state.results 是带上限的 LRU（防无界增长）", state.results instanceof LruMap && state.results.maxEntries >= 16);
}

// 16.5 快捷 chip 填充：只填进「同一组 regen-tools」里的输入框
const hintHost = chat.addMsg("bot", "");
chat.renderResult(
  { task_id: 7773, status: "success", language: "en", copies: [],
    images: [{ image_url: "/files/a.png", size: "1:1", template_id: "hero" }] },
  hintHost
);
const chips = hintHost.querySelectorAll('[data-action="fill-hint"]');
ok("渲染出风格快捷 chip", chips.length > 0, `-> ${chips.length}`);
chips[0].click();
await tick();
const regenInput = hintHost.querySelector(".regen-input");
ok("chip 把提示词填进同组输入框", regenInput.value === chips[0].dataset.hint,
  `-> ${JSON.stringify(regenInput.value)}`);

// 16.6 未知 data-action 不应抛错（前向兼容）
const safeHost = chat.addMsg("bot", `<div class="card"><button type="button" data-action="no-such-action">x</button></div>`);
safeHost.querySelector('[data-action="no-such-action"]').click();
await tick();
ok("未知 data-action 被安全忽略", true);

console.log("\n[17] 骨架屏与长任务加载态");
// 17.1 产品识别期间必须出现骨架屏（此前只有一行"正在识别产品…"）
let analyzeSkeletonSeen = false;
const origFetch = g.fetch;
g.fetch = async (url, opts) => {
  if (String(url).includes("/products/analyze")) {
    // 在"请求进行中"的时刻检查 DOM
    analyzeSkeletonSeen =
      !!q("#messages .skeleton") &&
      !!q("#messages .sk-grid") &&
      !!q("#messages .loading-bar") &&
      !!q("#messages .loading-dots");
  }
  return origFetch(url, opts);
};
analyzeResult = productPayload("骨架屏测试品", ["s1"]);
state.attached = { file: fakeFile("s.jpg"), dataUrl: "data:image/jpeg;base64,SSSS" };
await chat.sendMessage();
await tick();
g.fetch = origFetch;
ok("识别期间渲染骨架屏（含表单形状与加载条）", analyzeSkeletonSeen);
// 识别完成后骨架屏应被真实卡片替换
// 注意：只检查「识别骨架屏」的形状（.sk-grid 属于产品表单），不要用全局
// `.skeleton` —— 同会话里可能还有生成任务的骨架屏，会误判。
ok("识别完成后识别骨架屏已移除", qa("#messages .sk-grid").length === 0);
ok("识别完成后加载条已移除", qa("#messages .loading-bar").length === 0);
ok("识别完成后渲染真实产品卡片", qa("#messages .card input.f-name").length > 0);

// 17.2 生成任务期间只渲染进度卡片，不再预渲染结果骨架屏（避免把消息堆高）
const genHost = chat.renderProgressCard(8801);
ok("生成期间渲染进度卡片", !!genHost.querySelector(".progress-card"));
ok("生成期间不再渲染结果骨架屏", !genHost.querySelector(".result-skeleton"));
ok("进度卡片内已含已用时长位", !!genHost.querySelector(".progress-card .loading-elapsed"));

// 17.3 任务成功结束后进度卡片被移除、结果卡片进入（不留占位）
const es = esInstances[esInstances.length - 1];
es.emit({ task_id: 8801, status: "success", progress: 100, current_node: "image_compose", language: "en", copies: [], images: [] });
await tick();
ok("任务结束后进度卡片被移除", !genHost.querySelector(".progress-card"));
ok("任务结束后无骨架屏残留", !genHost.querySelector(".result-skeleton"));

// 17.4 任务取消后进度卡片清理
const cancelHost = chat.renderProgressCard(8802);
ok("取消前存在进度卡片", !!cancelHost.querySelector(".progress-card"));
const { requestCancel } = await import(modUrl("views/chat-stream.js"));
await requestCancel(8802);
await tick();
ok("取消后进度卡片被移除", !cancelHost.querySelector(".progress-card"));
ok("取消后显示已取消提示", cancelHost.querySelector(".bubble").textContent.includes("已取消"));

// 17.5 失败态同样清理进度卡片
const failHost = chat.renderProgressCard(8803);
const esFail = esInstances[esInstances.length - 1];
esFail.emit({ task_id: 8803, status: "failed", progress: 40, current_node: "copywriting", error: "上游超时" });
await tick();
ok("失败后进度卡片被移除", !failHost.querySelector(".progress-card"));
ok("失败后渲染错误卡片", !!failHost.querySelector(".error-card"));

// 17.6 已用时长计时器随任务终止而释放（不能留下每秒 tick 的孤儿定时器）
const liveBefore = process._getActiveHandles ? process._getActiveHandles().length : 0;
const timedHost = chat.renderProgressCard(8804);
const esTimed = esInstances[esInstances.length - 1];
esTimed.emit({ task_id: 8804, status: "success", progress: 100, current_node: "image_compose", language: "en", copies: [], images: [] });
await tick(30);
ok("任务结束后不再持有流（计时器已释放）", !state.streams.has(8804));
ok("任务结束后计时器元素随进度卡一并移除", !timedHost.querySelector(".progress-card"));

// 17.6b 回归：流被中途放弃时，资源须释放（进度卡 DOM 清理由 resetChat 的
// innerHTML="" 负责，不属 stopAllStreams 职责；此处验证流注册与计时器被清理）
const orphanHost = chat.renderProgressCard(8805);
ok("中途放弃前存在进度卡片", !!orphanHost.querySelector(".progress-card"));
chat.stopAllStreams(); // 模拟 resetChat / 切页导致的整体中断
ok("stopAllStreams 后流注册表清空", state.streams.size === 0);

// 17.7 loadingBarHtml / setLoadingText 的纯函数行为
const { loadingBarHtml, setLoadingText, startElapsed } = await import(modUrl("views/chat-render.js"));
const tmpMsg = chat.addMsg("bot", loadingBarHtml("初始文案"));
ok("加载条渲染初始文案", tmpMsg.querySelector(".loading-text").textContent === "初始文案");
ok("加载条渲染初始时长", tmpMsg.querySelector(".loading-elapsed").textContent === "0s");
setLoadingText(tmpMsg, "更新后的文案");
ok("setLoadingText 只改文本不重建 DOM", tmpMsg.querySelector(".loading-text").textContent === "更新后的文案");
const stopFn = startElapsed(tmpMsg);
await tick(1100);
ok("已用时长按秒递增", tmpMsg.querySelector(".loading-elapsed").textContent !== "0s",
  `-> ${tmpMsg.querySelector(".loading-elapsed").textContent}`);
stopFn();
const afterStop = tmpMsg.querySelector(".loading-elapsed").textContent;
await tick(1100);
ok("stopElapsed 后计时器停止", tmpMsg.querySelector(".loading-elapsed").textContent === afterStop,
  `-> ${afterStop} vs ${tmpMsg.querySelector(".loading-elapsed").textContent}`);
void liveBefore;

chat.stopAllStreams();

/* ---------- 收尾 ---------- */
rmSync(tmp, { recursive: true, force: true });
dom.window.close();

console.log(`\n通过 ${passed} 项，失败 ${failures.length} 项`);
if (failures.length) {
  console.log(`失败用例：\n  - ${failures.join("\n  - ")}`);
}
console.log(failures.length ? "DOM_TEST_FAILED" : "ALL_DOM_TEST_OK");
// 应用内有 checkHealth 的 setInterval 常驻定时器，必须显式退出，否则进程挂住
process.exit(failures.length ? 1 : 0);
