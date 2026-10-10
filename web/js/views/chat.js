/* ============================================================
   聊天主流程：识别 → 投放参数 → 进度(SSE) → 结果 → 局部重生成

   本文件是**对外门面（facade）**：其它模块与测试只需从这里 import，
   具体实现按职责拆到三个子模块，避免单文件 600+ 行混杂：

     chat-render.js  纯渲染（数据 → DOM，不绑事件）
     chat-stream.js  SSE / 轮询 / 取消等任务流生命周期
     chat-tools.js   动态卡片的事件委托（[data-action] 路由）

   约定：动态插入的卡片一律使用「类名 + 消息容器内作用域查询」，
   绝不使用 document 级 id（同一条会话里卡片会重复出现，重复 id 会让
   后续卡片的事件绑到第一张卡片上）。
   ============================================================ */
import { $, toast, scrollIntoViewChat } from "../core/dom.js?v=7d6d0fde";
import { fetchJson } from "../core/api.js?v=7d6d0fde";
import { state } from "../core/state.js?v=7d6d0fde";
import { clearAttach, maxFileMb } from "./composer.js?v=7d6d0fde";

import {
  addMsg, bubble, addUserMsg, productCardHtml, paramsCardHtml,
  resultHtml, errorCardHtml, guideHtml, paramsInfoHtml,
  installImageFallback, analyzeSkeletonHtml, setLoadingText, startElapsed,
} from "./chat-render.js?v=7d6d0fde";
import {
  renderProgressCard, stopStream, stopAllStreams, bindStreamRenderers,
} from "./chat-stream.js?v=7d6d0fde";
import { installCardActions, bindFlowHandlers } from "./chat-tools.js?v=7d6d0fde";

/* ---------- 对外再导出：保持既有 import 点无需改动 ---------- */
export { addMsg, bubble, renderProgressCard, stopStream, stopAllStreams, installImageFallback };

/* ============================================================
   流程 1：上传 + 产品识别
   ============================================================ */
export async function sendMessage() {
  if (state.analyzing) return;

  const input = $("chatInput");
  const hint = input.value.trim();
  const attach = state.attached;

  if (!attach) {
    addMsg("bot", `请先点击输入框左侧 <b>＋</b> 上传产品图片${hint ? "，再发送补充说明" : ""}。我会先识别产品，再帮你生成广告。`);
    return;
  }
  if (attach.file.size > maxFileMb() * 1024 * 1024) {
    toast(`图片超过 ${maxFileMb()}MB 限制`);
    return;
  }

  addUserMsg(hint || "请识别图中的产品", attach.dataUrl);
  input.value = "";
  input.style.height = "auto";
  clearAttach();

  // 骨架屏 + 已用时长：识别最长 180s，光靠一行"正在识别"用户无法区分卡死与在跑
  // （放宽上传上限后，大图在慢速移动网络上的上传耗时也要计入这个预算）
  const typing = addMsg("bot", analyzeSkeletonHtml());
  const stopElapsed = startElapsed(typing);
  // 超过 20s 追加安抚文案（识别大图时属正常，但要让用户知道没挂）
  const slowTimer = setTimeout(() => {
    setLoadingText(typing, "图片较大，仍在识别中");
  }, 20000);

  const sendBtn = $("btnSend");
  state.analyzing = true;
  sendBtn.disabled = true;
  try {
    const fd = new FormData();
    fd.append("file", attach.file);
    if (hint) fd.append("hint", hint);
    const result = await fetchJson("/products/analyze", { method: "POST", body: fd, timeout: 180000 });

    state.product = result.product;
    state.productId = result.product.id;
    typing.remove();
    addMsg("bot", productCardHtml(result));
  } catch (err) {
    typing.remove();
    bubble(addMsg("bot", "")).innerHTML = errorCardHtml("识别失败", err.message)
      .replace(
        "</div></div>",
        `</div><div class="card-actions"><button class="btn" type="button" data-action="pick-file">重新上传</button></div></div>`
      );
  } finally {
    clearTimeout(slowTimer);
    stopElapsed();
    state.analyzing = false;
    sendBtn.disabled = false;
  }
}

/* ============================================================
   流程 2：投放参数卡片 → 提交生成
   ============================================================ */
export function renderParamsCard() {
  const div = addMsg("bot", paramsCardHtml());
  // 用户点「确认信息」主动推进 → 强制滚到新卡片（scrollBottom 的 isNearBottom 守卫
  // 会因新卡片刚插入而把距离量得超大，从而拒绝滚动，导致"卡片出现但页面没跟着走"）
  scrollIntoViewChat(div);
}

/** 提交生成任务（由 chat-tools 的 data-action="generate" 调用） */
async function generateTask(card, host, btn) {
  btn.disabled = true;
  btn.textContent = "提交中…";
  const versionsSel = card.querySelector(".p-versions");
  try {
    const data = await fetchJson("/ads/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        product_id: state.productId,
        // 产品卡片上的修正随请求透传（此前被静默丢弃）
        product_override: {
          name: state.product?.name || "",
          category: state.product?.category || "",
          material: state.product?.material || "",
          color: state.product?.color || "",
          selling_points: state.product?.selling_points || [],
        },
        country: card.querySelector(".p-country").value,
        language: card.querySelector(".p-language").value,
        platform: card.querySelector(".p-platform").value,
        style: card.querySelector(".p-style").value,
        price: state.price || null,
        promotion: state.promotion || null,
        size_preset: card.querySelector(".p-size").value,
        num_versions: Number(versionsSel.value) || 3,
      }),
    });
    btn.disabled = false;
    btn.textContent = "✨ 生成广告";
    renderProgressCard(data.task_id);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "✨ 生成广告";
    addMsg("bot", errorCardHtml("任务提交失败", err.message));
  }
}

/* ============================================================
   流程 4：结果渲染
   ============================================================ */
export function renderResult(data, div) {
  // 结果快照按 taskId 存档，供事件委托回查（同会话多张卡片不串数据）
  const taskId = data.task_id;
  if (taskId != null) state.results.set(taskId, data);
  bubble(div).insertAdjacentHTML("beforeend", resultHtml(data));
  scrollIntoViewChat(div); // 结果出现时强制滚到它（scrollBottom 守卫会误判为"没靠近底部"）
}

export function renderError(data, div, title) {
  bubble(div).insertAdjacentHTML("beforeend", errorCardHtml(title, data.error));
}

/* ---------- 局部重生成 ---------- */

async function regenerateCopy({ copy, headline, button, host }) {
  const taskId = currentTaskId(host);
  button.disabled = true;
  try {
    const res = await fetchJson(`/ads/${taskId}/regenerate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "copy", edited_copy: { ...copy, headline } }),
    });
    renderProgressCard(res.task_id);
  } catch (err) {
    toast(err.message);
  } finally {
    button.disabled = false;
  }
}

async function regenerateImage(taskId, hint, _host, button) {
  const div = addMsg("bot", `<div>🎨 正在按要求<em>重新生成广告图</em>：</div>`);
  button.disabled = true;
  try {
    const res = await fetchJson(`/ads/${taskId}/regenerate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "image", edited_copy: null, visual_hint: hint || "" }),
    });
    renderProgressCard(res.task_id, div);
  } catch (err) {
    bubble(div).insertAdjacentHTML("beforeend", errorCardHtml("重新生成失败", err.message));
  } finally {
    button.disabled = false;
  }
}

const currentTaskId = (host) => {
  const card = host && host.querySelector(".card[data-task]");
  return card ? Number(card.dataset.task) : state.historyId;
};

/* ============================================================
   初始化（装配三块子模块）
   ============================================================ */
export function initChat() {
  // 接线：stream 需要在结束时调用结果/错误渲染
  bindStreamRenderers({ renderResult, renderError });
  // 接线：tools 需要调用流程动作
  bindFlowHandlers({
    toParams: () => renderParamsCard(),
    generate: generateTask,
    regenCopy: regenerateCopy,
    regenImage: regenerateImage,
  });
  installCardActions();
  installImageFallback();
}

/* ============================================================
   会话重置 / 快捷芯片
   ============================================================ */
export function resetChat(scroll = true) {
  $("messages").innerHTML = "";
  $("emptyState").style.display = "";
  state.product = null;
  state.productId = null;
  state.historyId = null;
  state.results.clear();
  stopAllStreams();
  clearAttach();
  if (scroll) $("chatScroll").scrollTop = 0;
}

export function chipGuide() {
  addMsg("bot", guideHtml());
}

export function chipParams() {
  addMsg("bot", paramsInfoHtml());
}

export function chipHint() {
  $("chatInput").focus();
  toast("输入补充说明后，点击 ＋ 上传产品图并发送");
}
