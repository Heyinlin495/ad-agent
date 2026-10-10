/* ============================================================
   应用外壳：视图切换 + 侧边栏
   ============================================================ */
import { $, qa } from "./core/dom.js?v=7d6d0fde";
import { loadKbDocs } from "./views/kb.js?v=7d6d0fde";
import { loadSettings } from "./views/settings.js?v=7d6d0fde";

const SIDEBAR_KEY = "adagent.sidebar.collapsed";

/**
 * 视图内容懒加载守卫：知识库列表较重（文档数可能多），只在**首次成功加载后**
 * 缓存，之后切回不再重复请求，避免每次导航都重拉一遍。
 * - 失败（后端未就绪）时不置位，下次进入会重试。
 * - 知识库的增删（upload/delete）内部已显式调用 loadKbDocs 刷新，不依赖这里。
 * 设置页是轻量只读快照，保持每次进入刷新（配置可能变化，宁可拉一次新的）。
 */
const kbLoaded = { done: false };

export function switchView(view) {
  qa(document, ".nav-item").forEach((n) => {
    const on = n.dataset.view === view;
    n.classList.toggle("active", on);
    if (on) n.setAttribute("aria-current", "page");
    else n.removeAttribute("aria-current");
  });
  // 只切 active 类、不重建 HTML → 各视图 DOM/输入状态天然保留（chat 消息等）
  qa(document, ".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "kb" && !kbLoaded.done) {
    loadKbDocs().then((ok) => { kbLoaded.done = ok; });
  }
  if (view === "settings") loadSettings();
}

export function collapseSidebar(collapsed) {
  $("sidebar").classList.toggle("hidden", collapsed);
  $("btnExpand").hidden = !collapsed;
  try {
    localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0");
  } catch {
    /* 隐私模式下 localStorage 不可用，忽略 */
  }
}

export function initShell() {
  qa(document, ".nav-item").forEach((n) => {
    n.onclick = () => switchView(n.dataset.view);
  });
  $("btnCollapse").onclick = () => collapseSidebar(true);
  $("btnExpand").onclick = () => collapseSidebar(false);

  // 恢复上次的侧边栏状态；移动端默认收起（否则会盖住整个内容区）
  let stored = null;
  try {
    stored = localStorage.getItem(SIDEBAR_KEY);
  } catch {
    stored = null;
  }
  const smallScreen = window.matchMedia("(max-width: 900px)").matches;
  collapseSidebar(stored === null ? smallScreen : stored === "1");
}
