/* ============================================================
   任务流生命周期：进度卡片挂载 + SSE 主通道 + 看门狗轮询兜底 + 取消

   每个任务持有独立的 EventSource / 轮询定时器，互不干扰。
   渲染部分委托给 chat-render.js，结果落地后的按键交互委托给 chat-tools.js。
   ============================================================ */
import { $, q, toast, isNearBottom, scrollIntoViewChat } from "../core/dom.js?v=2c2bf4b0";
import { api, request, postJson } from "../core/api.js?v=2c2bf4b0";
import { state } from "../core/state.js?v=2c2bf4b0";
import { hooks } from "../core/wiring.js?v=2c2bf4b0";
import { addMsg, bubble, progressCardHtml, updateProgress, startElapsed } from "./chat-render.js?v=2c2bf4b0";

/** 终止态集合 */
const TERMINAL = ["success", "failed", "cancelled"];

/** 结果渲染与错误渲染由 chat-flow 注入，避免 stream ←→ flow 循环依赖 */
let onResult = () => {};
let onError = () => {};

export function bindStreamRenderers({ renderResult, renderError }) {
  onResult = renderResult;
  onError = renderError;
}

export function stopStream(taskId) {
  const entry = state.streams.get(taskId);
  if (!entry) return;
  if (entry.es) entry.es.close();
  if (entry.timer) clearTimeout(entry.timer);
  // 释放随流创建的资源（已用时长计时器等），避免取消后仍每秒 tick
  for (const fn of entry.cleanup || []) {
    try { fn(); } catch { /* 单个清理失败不应影响其它 */ }
  }
  state.streams.delete(taskId);
}

export function stopAllStreams() {
  for (const taskId of Array.from(state.streams.keys())) stopStream(taskId);
}

export function renderProgressCard(taskId, hostDiv) {
  const div = hostDiv || addMsg("bot", `<div>⏳ 广告生成中，任务 <b>#${taskId}</b>：</div>`);
  if (!hostDiv) scrollIntoViewChat(div); // 用户点「生成」主动发起 → 强制滚到进度卡
  const box = bubble(div);
  box.insertAdjacentHTML("beforeend", progressCardHtml());
  const card = box.lastElementChild; // 精确指向本次插入的进度卡片
  if (card) card.dataset.task = String(taskId);

  // 不预渲染「结果骨架屏」占位：生成进度卡（进度条 + 7 阶段 + 已用时长）已足够
  // 证明后端在推进，再加一张空骨架屏只会把整条消息整体堆高、增大纵向占用。
  // 结果卡片在任务成功终态时由 onResult 直接插入即可。

  const finish = startStream(taskId, div);
  // 取消按钮的点击由 chat-tools 的事件委托统一处理（见 data-action="cancel"）
  // finish 已随 entry 存入 state.streams（TaskController），无需另设平行注册表
  return div;
}

function startStream(taskId, div) {
  stopStream(taskId); // 幂等：同一任务重复订阅时先释放旧连接

  let finished = false;
  let lastEventAt = Date.now();
  let errCount = 0; // 连续 SSE 错误计数，用于对浏览器自动重连设退避上限
  const entry = { es: null, timer: null, cleanup: [], finish: null };
  state.streams.register(taskId, entry);

  /* 已用时长：挂在进度卡片上，证明后端仍在工作 */
  const stopElapsed = startElapsed(div);
  entry.cleanup.push(stopElapsed);

  const finish = (data) => {
    if (finished) return;
    finished = true;
    const wasNearBottom = isNearBottom($("chatScroll"));
    stopStream(taskId); // 会执行 cleanup，骨架屏与计时器一并释放；entry 随之从注册表删除

    const card = q(div, ".progress-card");
    if (card) card.remove();

    if (data.status === "success") onResult(data, div);
    else if (data.status === "failed") onError(data, div, "生成失败");
    else bubble(div).insertAdjacentHTML("beforeend", `<div class="card"><div>🛑 任务已取消。</div></div>`);

    if (data.status === "success" && !wasNearBottom) toast("✅ 生成完成，结果已更新");
    hooks.refreshHistory();
  };
  entry.finish = finish; // 收敛到 entry：让 requestCancel 可从 state.streams.get(taskId).finish 取回调

  const onData = (data) => {
    lastEventAt = Date.now();
    updateProgress(div, data);
    if (TERMINAL.includes(data.status)) finish(data);
  };

  /* 主通道：SSE 实时推送 */
  const es = new EventSource(api(`/ads/${taskId}/stream`));
  entry.es = es;
  es.onmessage = (ev) => {
    try {
      onData(JSON.parse(ev.data));
      errCount = 0; // 收到事件说明连接恢复，重置错误计数
    } catch {
      /* 坏包忽略 */
    }
  };
  /* SSE 错误退避上限：EventSource 断线后会**自动无限重连且无退避**。
     这里跟踪连续错误，超过阈值就主动 close 放弃 SSE，把通道完全交给下方
     看门狗轮询（轮询每 1.2s 探测、SSE 3s 无事件即接管，足够可靠）。
     成功收到任一消息时 errCount 归零，允许短暂抖动后自愈。 */
  es.onerror = () => {
    errCount += 1;
    if (errCount >= 3 && entry.es === es) {
      es.close(); // 放弃 SSE：无限重连既占连接又反复打穿代理缓冲
    }
  };

  /* 兜底通道：活跃看门狗。后端每 0.5s 推送一次，若 SSE 超过 3s 无任何事件
     （代理只放行首个事件后缓冲，EventSource 不重连也不报错），主动轮询接管 */
  entry.timer = setTimeout(function poll() {
    if (finished) return;
    if (Date.now() - lastEventAt > 3000) {
      request(`/ads/${taskId}`).then(onData).catch(() => { /* 网络抖动，下一轮再试 */ });
    }
    if (!finished) entry.timer = setTimeout(poll, 1200);
  }, 1200);

  return finish;
}

/** 取消任务：请求后端置取消，然后本地立即收敛（不等 SSE 回包） */
export async function requestCancel(taskId, btn) {
  const entry = state.streams.get(taskId);
  const finish = entry && entry.finish;
  if (btn) {
    if (btn.dataset.requested === "1") return;
    btn.dataset.requested = "1";
    btn.disabled = true;
    btn.textContent = "取消中…";
  }
  try {
    await postJson(`/ads/${taskId}/cancel`, {});
  } catch {
    /* 任务可能已结束，忽略 */
  }
  if (finish) finish({ status: "cancelled" });
}
