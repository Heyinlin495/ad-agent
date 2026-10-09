/* ============================================================
   AdAgent Web 入口：装配各模块、绑定事件、启动探测

   目录：
     core/    基础设施（api / dom / state / modal / wiring）
     views/   业务视图（composer / chat / history / kb / settings）
     shell.js 视图切换与侧边栏
   ============================================================ */
import { $, installGlobalErrorHandlers, toast } from "./core/dom.js?v=2c2bf4b0";
import { request, probeApiBase, setApiBase, getApiBase } from "./core/api.js?v=2c2bf4b0";
import { state } from "./core/state.js?v=2c2bf4b0";
import { hooks } from "./core/wiring.js?v=2c2bf4b0";
import { initLightbox, initImagePreview } from "./core/modal.js?v=2c2bf4b0";
import { initShell, switchView } from "./shell.js?v=2c2bf4b0";
import { initComposer, pickFile, autoGrow } from "./views/composer.js?v=2c2bf4b0";
import { initKb } from "./views/kb.js?v=2c2bf4b0";
import { loadHistory, initHistory } from "./views/history.js?v=2c2bf4b0";
import { sendMessage, resetChat, chipGuide, chipParams, chipHint, initChat } from "./views/chat.js?v=2c2bf4b0";

/* ---------- 健康检查 ---------- */
async function checkHealth() {
  const dot = $("healthDot");
  const text = $("healthText");
  try {
    const h = await request("/health", { timeout: 8000 });
    dot.className = "dot ok";
    text.textContent = `${h.app} v${h.version} · 已连接`;
  } catch {
    dot.className = "dot err";
    text.textContent = "后端未连接";
  }
}

/* 上传上限以服务端配置为准（避免前端硬编码与后端不一致） */
async function loadRuntimeConfig() {
  try {
    const s = await request("/settings", { timeout: 10000 });
    if (s && Number(s.max_upload_size_mb) > 0) state.maxFileMb = Number(s.max_upload_size_mb);
  } catch {
    /* 取不到就用兜底值 */
  }
}

/* ---------- 事件绑定 ---------- */
function bind() {
  $("btnSend").onclick = sendMessage;
  $("chatInput").addEventListener("keydown", (e) => {
    // Enter 发送 / Shift+Enter 换行；输入法组合期间不拦截
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMessage();
    }
  });

  $("chipGuide").onclick = chipGuide;
  $("chipParams").onclick = chipParams;
  $("chipHint").onclick = chipHint;

  $("btnNewChat").onclick = () => {
    resetChat();
    switchView("chat");
    autoGrow();
  };
  $("btnRefreshHistory").onclick = loadHistory;

  // 事件委托：替换原先写在 HTML 字符串里的 inline onclick
  document.addEventListener("click", (e) => {
    const el = e.target.closest('[data-action="pick-file"]');
    if (el) pickFile();
  });

  initShell();
  initComposer();
  initKb();
  initHistory();
  initLightbox();
  initImagePreview();
  // 聊天模块装配：注入 stream/flow 的运行时回调，并注册卡片事件委托
  // （installImageFallback 由 initChat 内部调用，无需在此重复装配）
  initChat();

  // 跨模块接线：任务结束后刷新侧边栏历史
  hooks.refreshHistory = loadHistory;

  // 浏览器控制台切换后端地址（原实现用 alert，这里改为轻提示）
  window.setApiBase = (base) => {
    setApiBase(base);
    toast(`API 地址已切换为：${base || "(同源代理)"}`);
    checkHealth();
    loadHistory();
  };

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) checkHealth();
  });
}

/* ---------- 启动 ---------- */
installGlobalErrorHandlers();
bind();

probeApiBase().then(async () => {
  await loadRuntimeConfig();
  checkHealth();
  loadHistory();
  setInterval(checkHealth, 30000);
});

// 调试信息（控制台可见，便于排查 API 地址问题）
console.info(`[AdAgent] API_BASE = ${getApiBase() || "(同源)"}`);
