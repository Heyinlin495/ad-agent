/* ============================================================
   历史对话：列表 + 详情回放 + 删除
   ============================================================ */
import { COUNTRIES } from "../constants.js?v=7d6d0fde";
import { request } from "../core/api.js?v=7d6d0fde";
import { $, esc, toast } from "../core/dom.js?v=7d6d0fde";
import { state } from "../core/state.js?v=7d6d0fde";
import { appConfirm } from "../core/modal.js?v=7d6d0fde";
import { switchView } from "../shell.js?v=7d6d0fde";
import { addMsg, bubble, renderResult, renderError, resetChat } from "./chat.js?v=7d6d0fde";
import { resultSkeletonHtml } from "./chat-render.js?v=7d6d0fde";

const STATUS_TEXT = {
  success: "完成", failed: "失败", running: "进行中",
  pending: "排队", cancelled: "已取消",
};

const statusClass = (s) =>
  s === "success" ? "success"
    : s === "failed" ? "failed"
      : s === "running" || s === "pending" ? "running" : "other";

let historyInstalled = false;

/**
 * 历史列表中「查看 / 删除」改为在 #historyList 上做容器级事件委托：
 * 列表每次 innerHTML 重建后无需重新绑定，避免漏绑导致按钮失效。
 * 幂等：重复调用只挂一次监听。
 */
export function initHistory() {
  if (historyInstalled) return;
  const list = $("historyList");
  if (!list) return;
  historyInstalled = true;

  list.addEventListener("click", async (e) => {
    const item = e.target.closest("[data-action]");
    if (!item || !list.contains(item)) return;
    const id = Number(item.dataset.id);
    if (item.dataset.action === "hist-open") {
      openHistory(id);
    } else if (item.dataset.action === "hist-del") {
      const el = item;
      const okToDelete = await appConfirm(
        `确定删除历史任务 #${id} 吗？该任务的记录与产出将一并删除。`,
        { title: "删除历史任务", okText: "删除", danger: true }
      );
      if (!okToDelete) return;
      try {
        await request(`/history/${id}`, { method: "DELETE" });
        toast(`已删除任务 #${id}`);
        if (state.historyId === id) resetChat(false);
        loadHistory();
      } catch (err) {
        toast(err.message);
      }
    }
  });
}

export async function loadHistory() {
  const list = $("historyList");
  try {
    const data = await request("/history?page=1&page_size=30", { timeout: 15000 });
    if (!data.items.length) {
      list.innerHTML = `<div class="side-empty">暂无历史任务</div>`;
      return;
    }
    list.innerHTML = data.items.map((t) => {
      const country = COUNTRIES[t.country] || t.country || "-";
      return `
        <div class="hist-row">
          <button type="button" class="hist-item" data-action="hist-open" data-id="${t.id}"
                  title="${esc(country)} · ${esc(t.platform)} · ${esc(t.language)} ｜ 点击查看">
            <span class="hist-id">#${t.id}</span>
            <span class="hist-label">${esc(country)} · ${esc(t.platform)}</span>
            <span class="hist-badge ${statusClass(t.status)}">${esc(STATUS_TEXT[t.status] || t.status)}</span>
          </button>
          <button type="button" class="hist-del" data-action="hist-del" data-id="${t.id}"
                  title="删除该历史任务" aria-label="删除历史任务 #${t.id}">✕</button>
        </div>`;
    }).join("");
  } catch {
    list.innerHTML = `<div class="side-empty">后端未连接</div>`;
  }
}

export async function openHistory(taskId) {
  switchView("chat");
  resetChat(false);
  state.historyId = taskId;

  // 骨架屏：历史详情同样要等一次网络往返，用形状预告"即将出现文案+广告图"
  const typing = addMsg("bot", `<div>🕘 正在加载任务 <b>#${taskId}</b>…</div>${resultSkeletonHtml()}`);
  try {
    const data = await request(`/history/${taskId}`, { timeout: 30000 });
    typing.remove();
    const country = COUNTRIES[data.country] || data.country || "-";
    const div = addMsg("bot", `<div>🕘 历史任务 <b>#${taskId}</b>（${esc(country)} · ${esc(data.platform || "-")}）：</div>`);

    if (data.status === "failed") {
      renderError(data, div, "该任务生成失败");
    } else if ((data.copies || []).length || (data.images || []).length) {
      renderResult(data, div);
    } else {
      bubble(div).insertAdjacentHTML("beforeend",
        `<div class="card"><div>任务状态：${esc(data.status)}，暂无产出内容。</div></div>`);
    }
  } catch (err) {
    typing.remove();
    addMsg("bot", `<div class="card error-card"><div class="card-title">⚠️ 加载失败</div><div>${esc(err.message)}</div></div>`);
  }
}
