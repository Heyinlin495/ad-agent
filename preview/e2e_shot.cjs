(async () => {
/* 端到端截图脚本：驱动真实 Edge 走一遍广告生成流程，沿途截图
 * 运行：NODE_PATH=<workspace>/node_modules node e2e_shot.mjs
 */
const { chromium } = require("playwright-core");
const path = require("node:path");

const OUT = "C:/Users/Administrator/Desktop/dianshang/ad-agent/preview";
const IMG = "C:/Users/Administrator/Desktop/dianshang/ad-agent/backend/data/tmp/pp.png";
const URL = "http://127.0.0.1:8502/";

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.setDefaultTimeout(180000);

try {
  await page.goto(URL, { waitUntil: "load" });
  await page.screenshot({ path: path.join(OUT, "shot_0_首页.png") });
  log("首页已截图");

  // 1. 上传产品图（直接对 #fileInput 设文件，等效于点击＋后选择）
  await page.setInputFiles("#fileInput", IMG);
  log("已选择文件，等待产品识别（真实 Kimi 视觉，可能 10~90s）…");

  // 识别完成：产品卡片出现（骨架 .sk-grid 消失且输入框可用）
  await page.waitForSelector("#messages input.f-name", { timeout: 180000 });
  await page.waitForFunction(() => !document.querySelector("#messages .sk-grid"), { timeout: 30000 });
  await page.screenshot({ path: path.join(OUT, "shot_1_识别完成.png") });
  log("产品识别卡片已截图");

  // 2. 确认信息 → 参数卡片
  await page.click('[data-action="to-params"]');
  await page.waitForSelector('[data-action="generate"]', { timeout: 15000 });
  await page.screenshot({ path: path.join(OUT, "shot_2_投放参数.png") });
  log("参数卡片已截图");

  // 3. 点击生成 → 进度卡片出现（本轮优化的主角）
  await page.click('[data-action="generate"]');
  await page.waitForSelector(".progress-card", { timeout: 30000 });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: path.join(OUT, "shot_3_生成中.png") });
  log("生成中进度卡片已截图（无骨架屏版本）");

  // 中途再截一张（阶段推进后）
  await page.waitForTimeout(25000);
  if (await page.$(".progress-card")) {
    await page.screenshot({ path: path.join(OUT, "shot_4_生成中_后段.png") });
    log("生成中后段已截图");
  }

  // 4. 等待生成完成（真实模式：7 阶段 + 出图，给足 9 分钟）
  log("等待生成完成…");
  await page.waitForSelector(".gallery-item, .error-card", { timeout: 540000 });
  await page.waitForTimeout(1500);
  const ok = await page.$(".gallery-item");
  await page.screenshot({ path: path.join(OUT, ok ? "shot_5_生成完成.png" : "shot_5_生成失败.png"), fullPage: true });
  log(ok ? "生成完成，结果页已截图（整页）" : "生成失败，错误卡片已截图");
} catch (err) {
  console.error("流程中断:", err.message);
  await page.screenshot({ path: path.join(OUT, "shot_x_中断现场.png"), fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  await browser.close();
}
})();
