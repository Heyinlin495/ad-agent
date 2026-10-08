/* ============================================================
   动态卡片事件委托（统一入口）

   为什么用委托而不是逐个 onclick：
   结果卡片里的按钮数量随文案版本数线性增长（复制/重生成 × N，加图片重生成），
   逐个绑定既让绑定成本随卡片数增长，也让"卡片重复出现"时容易把事件绑错对象。
   改为在 #messages 上挂一个委托，按 [data-action] 路由，作用域从 closest 卡片取得。

   注意：所有状态查询必须限定在**被点击卡片所在的消息容器内**，
   绝不使用 document 级 getElementById —— 同一会话会出现多张同构卡片。
   ============================================================ */
import { q, qa, toast, copyText } from "../core/dom.js?v=99b6e455";
import { postJson } from "../core/api.js?v=99b6e455";
import { state } from "../core/state.js?v=99b6e455";
import { requestCancel } from "./chat-stream.js?v=99b6e455";

/** 由 chat-flow 注入的"跑生成任务"回调，避免 tools ←→ flow 循环依赖 */
let runFlow = {
  toParams: () => {},
  generate: async () => {},
  regenCopy: async () => {},
  regenImage: async () => {},
};

export function bindFlowHandlers(handlers) {
  runFlow = { ...runFlow, ...handlers };
}

/** 从被点击元素向上找到所属的 .msg 容器 */
const hostOf = (el) => el.closest(".msg");

/** 从被点击元素向上找到最近的 .card */
const cardOf = (el) => el.closest(".card");

/** 委托是否已安装：重复调用 initChat 时不得重复挂监听，
 *  否则一次点击会按监听数量重复触发（曾出现点「确认信息」生成两张参数卡片）。 */
let installed = false;

export function installCardActions() {
  if (installed) return; // 幂等：重复装配直接返回
  const root = document.getElementById("messages");
  if (!root) return;
  installed = true;

  root.addEventListener("click", async (e) => {
    const el = e.target.closest("[data-action]");
    if (!el || !root.contains(el)) return;
    const action = el.dataset.action;
    const host = hostOf(el) || root;

    switch (action) {
      /* 产品卡片：确认信息 → 进入投放参数 */
      case "to-params": {
        const card = cardOf(el);
        if (!card) return;
        const val = (sel) => q(card, sel).value.trim();
        state.product = {
          ...state.product,
          name: val(".f-name"),
          category: val(".f-category"),
          material: val(".f-material"),
          color: val(".f-color"),
          selling_points: q(card, ".f-points").value.split("\n").map((s) => s.trim()).filter(Boolean),
        };
        state.price = val(".f-price");
        state.promotion = val(".f-promotion");
        runFlow.toParams(host);
        return;
      }

      /* 参数卡片：提交生成任务 */
      case "generate": {
        const card = cardOf(el);
        if (!card) return;
        await runFlow.generate(card, host, el);
        return;
      }

      /* 进度卡片：取消任务 */
      case "cancel": {
        const taskId = Number((cardOf(el) || host)?.dataset.task);
        if (taskId) await requestCancel(taskId, el);
        return;
      }

      /* 结果卡片：复制文案 */
      case "copy-copy": {
        const item = el.closest(".copy-item");
        const version = Number(el.dataset.version);
        const copy = findCopy(host, version);
        if (!copy) return;
        const text = [
          copy.headline, copy.subheadline, ...(copy.bullets || []), copy.cta,
          (copy.hashtags || []).join(" "),
        ].filter(Boolean).join("\n");
        copyText(text);
        return;
      }

      /* 结果卡片：按编辑后的标题重生成该版文案 */
      case "regen-copy": {
        const item = el.closest(".copy-item");
        const version = Number(el.dataset.version);
        const copy = findCopy(host, version);
        if (!copy) return;
        const input = item && q(item, ".ed-copy");
        await runFlow.regenCopy({
          copy,
          headline: input ? input.value.trim() : copy.headline,
          button: el,
          host,
        });
        return;
      }

      /* 结果卡片：换个风格重生成广告图 */
      case "regen-image": {
        const wrap = el.closest(".regen-tools");
        const input = wrap && q(wrap, ".regen-input");
        const card = cardOf(el);
        const taskId = Number(card && card.dataset.task);
        if (taskId) await runFlow.regenImage(taskId, input ? input.value.trim() : "", host, el);
        return;
      }

      /* 风格快捷 chip：把提示词填进同组输入框 */
      case "fill-hint": {
        const wrap = el.closest(".regen-tools");
        const input = wrap && q(wrap, ".regen-input");
        if (input) {
          input.value = el.dataset.hint || "";
          input.focus();
        }
        return;
      }

      default:
        return;
    }
  });

  /* 版本数下拉联动提示文案：change 事件在 .card 上委托 */
  root.addEventListener("change", (e) => {
    const sel = e.target.closest(".p-versions");
    if (!sel) return;
    // 提示文案在卡片外的同一气泡内，必须按消息容器作用域查找，不能用 card
    const host = hostOf(sel);
    const label = host && q(host, ".p-versions-label");
    if (label) label.textContent = sel.value;
  });
}

/** 在指定消息容器内找到指定版本号的文案对象（结果快照存在卡片上） */
function findCopy(host, version) {
  const card = host.querySelector(".card[data-task]");
  const taskId = card ? Number(card.dataset.task) : null;
  const snapshot = taskId != null ? state.results.get(taskId) : null;
  const copies = (snapshot && snapshot.copies) || [];
  return copies.find((x) => String(x.version_no) === String(version)) || null;
}

