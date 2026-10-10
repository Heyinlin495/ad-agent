/* ============================================================
   模态与图片预览：替代原生 confirm/alert，含焦点管理
   ============================================================ */
import { $, trapFocus } from "./dom.js?v=7d6d0fde";

let modalLastFocus = null;

/**
 * 打开模态框。返回 Promise：确定 → true，取消 / Esc / 点遮罩 → false。
 */
export function openModal({
  title = "提示",
  body = "",
  okText = "确定",
  cancelText = "取消",
  danger = false,
  showCancel = true,
} = {}) {
  return new Promise((resolve) => {
    const modal = $("modal");
    const okBtn = $("modalOk");
    const cancelBtn = $("modalCancel");

    $("modalTitle").textContent = title;
    $("modalBody").textContent = body;
    okBtn.textContent = okText;
    okBtn.className = danger ? "btn primary danger-solid" : "btn primary";
    cancelBtn.textContent = cancelText;
    cancelBtn.hidden = !showCancel;

    modalLastFocus = document.activeElement;
    modal.hidden = false;

    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        done(false);
      } else if (e.key === "Tab") {
        trapFocus(modal, e);
      }
    };

    function done(value) {
      modal.hidden = true;
      document.removeEventListener("keydown", onKey, true);
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      modal.onclick = null;
      if (modalLastFocus && typeof modalLastFocus.focus === "function") modalLastFocus.focus();
      modalLastFocus = null;
      resolve(value);
    }

    okBtn.onclick = () => done(true);
    cancelBtn.onclick = () => done(false);
    modal.onclick = (e) => {
      if (e.target === modal) done(false);
    };
    document.addEventListener("keydown", onKey, true);
    (showCancel ? cancelBtn : okBtn).focus();
  });
}

export const appConfirm = (message, opts = {}) =>
  openModal({ title: "请确认", body: message, okText: "确定", ...opts });

export const appAlert = (message, opts = {}) =>
  openModal({ title: "提示", body: message, okText: "知道了", showCancel: false, ...opts });

/* ---------- 图片预览 ---------- */
let lbLastFocus = null;

export function openLightbox(src) {
  const lb = $("lightbox");
  lbLastFocus = document.activeElement;
  $("lbImg").src = src;
  $("lbDownload").href = src;
  lb.hidden = false;
  $("lbClose").focus();
}

export function closeLightbox() {
  const lb = $("lightbox");
  if (lb.hidden) return;
  lb.hidden = true;
  $("lbImg").src = "";
  if (lbLastFocus && typeof lbLastFocus.focus === "function") lbLastFocus.focus();
  lbLastFocus = null;
}

export function initLightbox() {
  const lb = $("lightbox");
  $("lbClose").onclick = closeLightbox;
  lb.addEventListener("click", (e) => {
    if (e.target === lb) closeLightbox();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("modal").hidden) return; // 模态优先，避免同时关闭
    if (!lb.hidden) {
      e.preventDefault();
      closeLightbox();
    }
  });
  // 焦点陷阱 + Esc 需要按 Tab 循环时也留在遮罩内
  lb.addEventListener("keydown", (e) => {
    if (e.key === "Tab") trapFocus(lb, e);
  });
}

/** 图片预览：事件委托，画廊内任意图片点击均可触发（含历史记录加载的） */
export function initImagePreview() {
  document.addEventListener("click", (e) => {
    const img = e.target.closest("img[data-preview]");
    if (img) {
      e.preventDefault();
      openLightbox(img.dataset.preview);
    }
  });
}
