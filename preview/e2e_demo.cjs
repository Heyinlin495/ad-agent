/* 过程演示：驱动真实页面渲染生成各阶段并截图（无需等真实 LLM）
 * 复用项目真实模块：chat-stream.renderProgressCard / chat-render.updateProgress / resultHtml
 * 结果卡片使用历史任务 #1 的真实产出数据
 */
const { chromium } = require("playwright-core");
const path = require("node:path");

const OUT = "C:/Users/Administrator/Desktop/dianshang/ad-agent/preview";
const URL = "http://127.0.0.1:8502/";
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  try {
    await page.goto(URL, { waitUntil: "load" });

    // 注入：一条用户消息 + 生成进度卡（真实模块渲染）
    await page.evaluate(async () => {
      const V = "?v=62551c9a";
      const render = await import("/js/views/chat-render.js" + V);
      const stream = await import("/js/views/chat-stream.js" + V);
      window.__render = render;
      window.__stream = stream;
      render.addUserMsg("帮我生成这款产品的广告图，走欧美简约风", null);
      window.__div = stream.renderProgressCard(1024);
    });
    await page.waitForTimeout(600);

    // 阶段 1：产品分析 12%
    await page.evaluate(() => {
      window.__render.updateProgress(window.__div, { progress: 12, current_node: "analyze_image", status: "running" });
    });
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(OUT, "run_1_产品分析.png") });
    log("阶段1 产品分析 已截图");

    // 阶段 2：广告策划 46%
    await page.evaluate(() => {
      window.__render.updateProgress(window.__div, { progress: 46, current_node: "ad_plan", status: "running" });
    });
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(OUT, "run_2_广告策划.png") });
    log("阶段2 广告策划 已截图");

    // 阶段 3：图片生成 86%
    await page.evaluate(() => {
      window.__render.updateProgress(window.__div, { progress: 86, current_node: "image_compose", status: "running" });
    });
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(OUT, "run_3_图片生成.png") });
    log("阶段3 图片生成 已截图");

    // 收尾：撤进度卡，用历史任务 #1 的真实产出渲染结果卡片
    const detail = await page.evaluate(async () => {
      const res = await fetch("http://127.0.0.1:8000/api/v1/history/1");
      const j = await res.json();
      window.__stream.stopAllStreams();
      const pc = window.__div.querySelector(".progress-card");
      if (pc) pc.remove();
      const div = window.__render.addMsg("bot", window.__render.resultHtml(j.data));
      window.__resultDiv = div;
      return { status: j.data.status, copies: (j.data.copies || []).length, images: (j.data.images || []).length };
    });
    log("历史结果:", JSON.stringify(detail));
    await page.waitForTimeout(1200); // 等画廊图片加载
    await page.screenshot({ path: path.join(OUT, "run_4_生成完成.png"), fullPage: true });
    log("生成完成结果页 已截图（整页）");

    // 单独给「生成中进度卡」一个近景（1x 视觉更清楚）
    await page.evaluate(() => { window.scrollTo(0, 0); });
    log("全部完成");
  } catch (err) {
    console.error("中断:", err.message);
    await page.screenshot({ path: path.join(OUT, "run_x_中断现场.png"), fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
