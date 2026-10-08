/* ============================================================
   DOM / 通用 UI 工具
   ============================================================ */

export const $ = (id) => document.getElementById(id);
export const q = (root, sel) => {
  if (typeof root === "string") return document.querySelector(root); // 容错：q("sel") 同 $
  return (root || document).querySelector(sel);
};
export const qa = (root, sel) => {
  if (typeof root === "string") return Array.from(document.querySelectorAll(root));
  return Array.from((root || document).querySelectorAll(sel));
};

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/**
 * URL 协议白名单：只放行 https/http 与 data:image。
 * 阻止 `javascript:`、`vbscript:`、`data:text/html` 等可执行伪协议被塞进
 * src/href 后触发 XSS。不可信来源的 URL 一律先用它过滤。
 */
export function safeUrl(u) {
  const s = String(u ?? "").trim();
  if (!s) return "";
  // 去掉控制字符与空白（`java\nscript:` 这类绕过写法）
  const probe = s.replace(/[\u0000-\u0020\u007f]/g, "").toLowerCase();
  if (
    probe.startsWith("http://") ||
    probe.startsWith("https://") ||
    probe.startsWith("/") || // 站内相对路径（同源 /files/... 等）
    probe.startsWith("./") ||
    probe.startsWith("../") ||
    probe.startsWith("data:image/") ||
    probe.startsWith("blob:")
  ) {
    return s;
  }
  return "";
}

/** 生成唯一 id（用于动态卡片里的 label/for 关联，避免重复 id） */
let uidSeq = 0;
export const uid = (prefix) => `${prefix}-${++uidSeq}`;

/* ---------- 轻提示 ---------- */
export function toast(msg, ms = 2200) {
  const t = $("toast");
  if (!t) return;
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => (t.hidden = true), ms);
}

/* ---------- 滚动 ---------- */

/** 视口是否已接近底部（用户主动上滑阅读时不应被强行拉回） */
export function isNearBottom(el = $("chatScroll"), gap = 90) {
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight < gap;
}

let scrollRaf = 0;
export function scrollBottom(force = false) {
  const el = $("chatScroll");
  if (!el) return;
  if (!force && !isNearBottom(el)) return; // 用户在阅读历史，不打扰
  cancelAnimationFrame(scrollRaf);
  scrollRaf = requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
}

/**
 * 把某个消息元素滚动到 chatScroll 可视区内（靠近底部）。
 * 为什么需要它：scrollBottom() 只在「接近底部」时才滚动，而用户主动点按钮触发
 * 下一步时，新卡片刚插入、scrollHeight 立即变大，isNearBottom 量到的距离早已远超阈值，
 * 导致页面「卡片出来了却停留原地」。这里按用户动作强制滚到新卡片顶部。
 */
export function scrollIntoViewChat(target) {
  const el = $("chatScroll");
  if (!el || !target) return;
  cancelAnimationFrame(scrollRaf);
  scrollRaf = requestAnimationFrame(() => {
    // 滚动到目标元素的 clientTop（相对 chatScroll 顶），并留出一点上边距
    const top = target.offsetTop - 16;
    el.scrollTop = Math.max(0, top);
  });
}

/* ---------- 剪贴板（http 非安全上下文下 navigator.clipboard 不可用） ---------- */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      toast("文案已复制");
      return true;
    }
    throw new Error("clipboard unavailable");
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      toast(ok ? "文案已复制" : "复制失败，请手动选择");
      return ok;
    } catch {
      toast("复制失败，请手动选择");
      return false;
    }
  }
}

/* ---------- 全局错误兜底 ---------- */
let lastErrorToastAt = 0;

export function installGlobalErrorHandlers() {
  const report = (detail) => {
    // 节流：同一批异常只提示一次，避免刷屏
    const now = Date.now();
    if (now - lastErrorToastAt < 3000) return;
    lastErrorToastAt = now;
    toast(`页面出现异常：${detail}`, 4000);
    console.error("[AdAgent]", detail);
  };

  window.addEventListener("error", (e) => {
    if (e.message) report(e.message);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    report((r && (r.message || String(r))) || "未处理的异步错误");
  });
}

/* ---------- 焦点陷阱（模态 / lightbox 共用） ---------- */
const FOCUS_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function trapFocus(container, event) {
  const items = qa(container, FOCUS_SELECTOR).filter((el) => el.offsetParent !== null || el === document.activeElement);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
