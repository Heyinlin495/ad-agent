/* ============================================================
   消息渲染（纯函数 + DOM 插入，不做事件绑定）

   职责边界：只负责"把数据变成 DOM"，不订阅流、不绑定业务事件。
   事件绑定统一在 chat-tools.js 里按 [data-action] 委托处理。

   约定：动态插入的卡片一律使用「类名 + 消息容器内作用域查询」，
   绝不使用 document 级 id（同一条会话里卡片会重复出现，重复 id 会让
   后续卡片的事件绑到第一张卡片上）。
   ============================================================ */
import {
  COUNTRIES, LANGUAGE_NAMES, PLATFORMS, STYLES, SIZES,
  FLOW_NODES, NODE_NAMES, NODE_INDEX, REGEN_HINTS, RTL_LANGUAGES,
} from "../constants.js?v=99b6e455";
import { $, esc, safeUrl, q, uid, scrollBottom } from "../core/dom.js?v=99b6e455";
import { state } from "../core/state.js?v=99b6e455";
import { fileUrl } from "../core/api.js?v=99b6e455";
import { maxFileMb } from "./composer.js?v=99b6e455";

/* ---------- 基础消息 ---------- */

export function addMsg(role, html) {
  const wrap = $("messages");
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = `<div class="avatar" aria-hidden="true">${role === "bot" ? "A" : "我"}</div><div class="bubble">${html}</div>`;
  wrap.appendChild(div);
  $("emptyState").style.display = "none";
  scrollBottom();
  return div;
}

/** 取消息气泡容器（所有动态内容都往这里追加） */
export const bubble = (div) => q(div, ".bubble");

export function addUserMsg(text, dataUrl) {
  let inner = esc(text);
  // dataUrl 来自本地 FileReader，但仍按属性上下文转义：一旦上游改成远端 URL，
  // 未转义的 `"` 就能闭合 src 属性注入新属性（属性型 XSS）。
  // 这里额外用 safeUrl 只放行 http/https/data:image 三种协议，挡掉 javascript: 等伪协议。
  const safe = safeUrl(dataUrl);
  if (safe) inner += `<img class="msg-img" src="${esc(safe)}" alt="已上传的产品图" />`;
  return addMsg("user", inner);
}

/* ---------- 产品识别卡片 ---------- */

export function productCardHtml(result) {
  const p = result.product;
  const quality = result.quality || {};
  const risks = p.risk_flags || [];
  const qualityWarnings = quality.ok ? [] : quality.messages || [];

  const f = {
    name: uid("f-name"), category: uid("f-category"), material: uid("f-material"),
    color: uid("f-color"), price: uid("f-price"), promotion: uid("f-promotion"), points: uid("f-points"),
  };

  return `
    <div>✅ 识别完成，请核对产品信息：</div>
    <div class="card">
      <div class="card-title">📦 产品信息（可编辑）</div>
      <div class="form-grid">
        <div class="field"><label for="${f.name}">产品名称</label><input id="${f.name}" class="f-name" value="${esc(p.name)}" /></div>
        <div class="field"><label for="${f.category}">品类</label><input id="${f.category}" class="f-category" value="${esc(p.category)}" /></div>
        <div class="field"><label for="${f.material}">材质</label><input id="${f.material}" class="f-material" value="${esc(p.material || "")}" /></div>
        <div class="field"><label for="${f.color}">颜色</label><input id="${f.color}" class="f-color" value="${esc(p.color || "")}" /></div>
        <div class="field"><label for="${f.price}">价格（可选）</label><input id="${f.price}" class="f-price" value="${esc(state.price)}" placeholder="例如 $29.99" /></div>
        <div class="field"><label for="${f.promotion}">促销信息（可选）</label><input id="${f.promotion}" class="f-promotion" value="${esc(state.promotion)}" placeholder="例如 限时 8 折" /></div>
        <div class="field full"><label for="${f.points}">核心卖点（每行一条）</label><textarea id="${f.points}" class="f-points">${esc((p.selling_points || []).join("\n"))}</textarea></div>
      </div>
      ${risks.length ? `<div class="risk-banner">⚠️ 风险标记：${risks.map((x) => esc(x.description)).join("；")}</div>` : ""}
      ${qualityWarnings.length ? `<div class="risk-banner">🖼 图片质量：${qualityWarnings.map(esc).join("；")}</div>` : ""}
      <div class="card-actions">
        <button class="btn primary" type="button" data-action="to-params">确认信息，选择投放参数 →</button>
      </div>
    </div>`;
}

/* ---------- 投放参数卡片 ---------- */

export function paramsCardHtml() {
  const opt = (obj, sel) => Object.entries(obj)
    .map(([k, v]) => `<option value="${esc(k)}" ${k === sel ? "selected" : ""}>${esc(v)}</option>`).join("");
  const optArr = (arr, sel) => arr.map((v) => `<option ${v === sel ? "selected" : ""}>${esc(v)}</option>`).join("");

  const f = {
    country: uid("p-country"), language: uid("p-language"), platform: uid("p-platform"),
    style: uid("p-style"), versions: uid("p-versions"), size: uid("p-size"),
  };

  return `
    <div>🎯 请确认投放参数，我将生成 <b class="p-versions-label">3</b> 个文案版本及对应广告图：</div>
    <div class="card">
      <div class="card-title">🌍 投放参数</div>
      <div class="form-grid">
        <div class="field"><label for="${f.country}">目标国家</label><select id="${f.country}" class="p-country">${opt(COUNTRIES, "US")}</select></div>
        <div class="field"><label for="${f.language}">语言</label><select id="${f.language}" class="p-language">${opt(LANGUAGE_NAMES, "en")}</select></div>
        <div class="field"><label for="${f.platform}">投放平台</label><select id="${f.platform}" class="p-platform">${optArr(PLATFORMS, "Amazon")}</select></div>
        <div class="field"><label for="${f.style}">文案风格</label><select id="${f.style}" class="p-style">${opt(STYLES, "promo")}</select></div>
        <div class="field"><label for="${f.versions}">文案版本数</label><select id="${f.versions}" class="p-versions">${optArr(["1", "2", "3"], "3")}</select></div>
        <div class="field full"><label for="${f.size}">图片尺寸</label><select id="${f.size}" class="p-size">${opt(SIZES, "1:1")}</select></div>
      </div>
      <div class="card-actions">
        <button class="btn primary" type="button" data-action="generate">✨ 生成广告</button>
      </div>
    </div>`;
}

/* ---------- 进度卡片 ---------- */

export function progressCardHtml() {
  return `
    <div class="card progress-card">
      <div class="card-title">🚀 生成进度</div>
      <div class="progress-wrap">
        <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-label="广告生成进度">
          <div class="progress-fill pg-fill"></div>
        </div>
        <div class="progress-meta"><span class="pg-node">准备中…</span><span class="loading-elapsed">0s</span><span class="pg-pct">0%</span></div>
      </div>
      <div class="steps-line">${FLOW_NODES.map(([, n], i) => `<span class="step-tag" data-stage="${i}"><i class="step-idx">${i + 1}</i><span class="step-name">${esc(n)}</span></span>`).join("")}</div>
      <div class="card-actions"><button class="btn danger" type="button" data-action="cancel">取消任务</button></div>
    </div>`;
}

/** 按最新快照刷新进度卡片（纯展示） */
export function updateProgress(div, data) {
  const card = q(div, ".progress-card");
  if (!card) return;

  const pct = Math.max(0, Math.min(Number(data.progress) || 0, 100));
  const fill = q(card, ".pg-fill");
  if (fill) fill.style.width = `${pct}%`;
  const track = q(card, ".progress-track");
  if (track) track.setAttribute("aria-valuenow", String(pct));

  const nodeEl = q(card, ".pg-node");
  if (nodeEl) nodeEl.textContent = NODE_NAMES[data.current_node] || data.current_node || "准备中…";
  const pctEl = q(card, ".pg-pct");
  if (pctEl) pctEl.textContent = `${pct}%`;

  const idx = NODE_INDEX[data.current_node];
  const running = data.status === "running";
  const succeeded = data.status === "success";
  Array.from(card.querySelectorAll(".step-tag")).forEach((tag, i) => {
    if (succeeded) tag.className = "step-tag done";
    else if (idx === undefined) tag.className = "step-tag";
    else if (i < idx) tag.className = "step-tag done";
    else if (i === idx && running) tag.className = "step-tag active";
    else tag.className = "step-tag";
  });

  scrollBottom(); // 用户上滑阅读时不打扰
}

/* ---------- 骨架屏 / 长任务加载态 ---------- */

/**
 * 长任务加载条：圆点 + 阶段文案 + 已用时长。
 * 用固定结构 + 类名更新（而不是每次重排 innerHTML），这样计时器可以只改文本。
 */
export function loadingBarHtml(text) {
  return `<div class="loading-bar"><span class="loading-dots" aria-hidden="true"><i></i><i></i><i></i></span>`
    + `<span class="loading-text">${esc(text)}</span>`
    + `<span class="loading-elapsed">0s</span></div>`;
}

/** 更新加载条文案（不重建 DOM，避免计时器每 tick 重排） */
export function setLoadingText(div, text) {
  const el = q(div, ".loading-text");
  if (el) el.textContent = text;
}

/**
 * 启动「已用时长」计时器，返回停止函数。
 * 为什么需要：长任务里用户最想知道的是"还要等多久/是不是卡住了"，
 * 秒表的稳定递增是唯一能证明"后端仍在工作"的直观信号。
 */
export function startElapsed(div) {
  const el = q(div, ".loading-elapsed");
  if (!el) return () => {};
  const t0 = Date.now();
  const tick = () => {
    const s = Math.floor((Date.now() - t0) / 1000);
    el.textContent = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  return () => clearInterval(timer);
}

/** 产品识别骨架屏：按即将出现的「表单」预告形状。
    刻意精简为窄高度（两小块 label/input + 一行卖点），
    避免识别阶段大灰块把画面撑得又大又长。 */
export function analyzeSkeletonHtml() {
  const cell = `<div class="sk-field"><div class="skeleton sk-label"></div><div class="skeleton sk-input"></div></div>`;
  return `
    <div>🔍 正在识别产品，请稍候…</div>
    <div class="card skeleton-card">
      <div class="sk-title-line">
        <div class="skeleton sk-line w50" style="margin:0"></div>
      </div>
      <div class="sk-grid">${cell}${cell}</div>
      ${loadingBarHtml("正在分析图片内容")}
    </div>`;
}

/** 结果骨架屏：生成中占位。只预告"即将出现文案 + 广告图"的轮廓。
    刻意保持精简（一行文案线 + 一排矮缩略图），避免大灰块把生成中的
    画面撑得过高，让进度卡片始终是视觉焦点。 */
export function resultSkeletonHtml() {
  const thumb = `<div class="sk-thumb"><div class="skeleton sk-thumb-in"></div></div>`;
  return `
    <div class="card">
      <div class="sk-title-line">
        <div class="skeleton sk-line w60" style="margin:0"></div>
        <div class="loading-dots" aria-hidden="true"><i></i><i></i><i></i></div>
      </div>
      <div class="sk-gallery-row">${thumb}${thumb}${thumb}</div>
    </div>`;
}

/* ---------- 结果卡片 ---------- */

function complianceBadge(c) {
  const passed = (c.compliance || {}).passed !== false;
  return `<span class="badge ${passed ? "success" : "failed"}">${passed ? "✓ 合规通过" : "⚠ 待复核"}</span>`;
}

function copyHtml(c, rtl) {
  const v = c.version_no ?? 0;
  const bullets = (c.bullets || []).map((b) => `<li>${esc(b)}</li>`).join("");
  const tags = [...(c.hashtags || []), ...(c.keywords || [])].map((t) => `<span class="tag">${esc(t)}</span>`).join("");
  const ragN = (c.rag_sources || []).length;
  return `
    <div class="copy-item" data-version="${v}"${rtl ? ' dir="rtl" lang="ar"' : ""}>
      <div class="copy-head">
        <h4>版本 ${v}</h4>
        ${complianceBadge(c)}
        ${ragN ? `<span class="badge neutral">📚 引用 ${ragN} 条知识</span>` : ""}
        <button class="btn" style="margin-left:auto" type="button" data-action="copy-copy" data-version="${v}">📋 复制文案</button>
      </div>
      <div class="copy-headline">${esc(c.headline)}</div>
      ${c.subheadline ? `<div class="copy-sub">${esc(c.subheadline)}</div>` : ""}
      ${bullets ? `<ul class="copy-bullets">${bullets}</ul>` : ""}
      ${c.cta ? `<span class="copy-cta">${esc(c.cta)}</span>` : ""}
      ${tags ? `<div class="copy-tags">${tags}</div>` : ""}
      <div class="copy-tools">
        <input class="text-input ed-copy" placeholder="改标题后点右侧重新生成…" value="${esc(c.headline)}" aria-label="编辑版本 ${v} 的标题" />
        <button class="btn" type="button" data-action="regen-copy" data-version="${v}">✎ 按新标题重生成</button>
      </div>
    </div>`;
}

function galleryHtml(images) {
  const items = images
    .map((im) => {
      const src = safeUrl(fileUrl(im.image_url));
      const label = im.size || im.template_id || "广告图";
      // 图片不可用（地址为空 / 协议非法）时给占位块，避免出现破图与空洞
      const media = src
        ? `<img src="${esc(src)}" alt="广告图 ${esc(label)}" loading="lazy" decoding="async"
                data-preview="${esc(src)}" data-fallback="1" />`
        : `<div class="gallery-ph" role="img" aria-label="广告图不可用">🖼 图片不可用</div>`;
      const dl = src ? `<a href="${esc(src)}" download>下载</a>` : `<span class="muted">无法下载</span>`;
      return `
        <div class="gallery-item">
          ${media}
          <div class="gallery-meta"><span>${esc(label)}</span>${dl}</div>
        </div>`;
    })
    .join("");
  if (!items.trim()) return "";
  return `
    <div class="card-title" style="margin-top:14px">🖼 广告图（${images.length} 张，点击可预览）</div>
    <div class="gallery">${items}</div>`;
}

/* 「重生成图片」控制条：改风格/背景/色调后重新生成广告图（不动文案） */
function regenImageHtml() {
  return `
    <div class="card-title" style="margin-top:14px">🎨 对广告图不满意？换个风格重生成</div>
    <div class="regen-tools">
      <div class="chip-row">
        ${REGEN_HINTS.map((s) => `<button class="chip-btn" type="button" data-action="fill-hint" data-hint="${esc(s)}">${esc(s)}</button>`).join("")}
      </div>
      <div class="card-actions" style="margin-top:8px">
        <input class="text-input regen-input" placeholder="或自定：如“柔和的奶油色背景，偏暖，杂志风格”…" aria-label="自定义重生成要求" />
        <button class="btn primary" type="button" data-action="regen-image">✨ 重新生成广告图</button>
      </div>
    </div>`;
}

/** 结果卡片 HTML（文案 + 画廊 + 重生成入口），事件由 chat-tools 委托 */
export function resultHtml(data) {
  const copies = data.copies || [];
  const images = data.images || [];
  const rtl = RTL_LANGUAGES.has(data.language);

  let html = `<div>✅ 生成完成！结果如下：</div><div class="card" data-task="${data.task_id ?? ""}">`;
  if (data.ad_theme) {
    html += `<div class="theme-line">🎯 广告主题：<span>${esc(data.ad_theme)}</span></div>`;
  }
  if (copies.length) {
    html += `<div class="card-title">✍️ 广告文案（${copies.length} 个版本）</div>`;
    html += copies.map((c) => copyHtml(c, rtl)).join("");
  }
  if (images.length) {
    html += galleryHtml(images);
    html += regenImageHtml();
  }
  if (!copies.length && !images.length) html += `<div>任务成功，但未返回内容。</div>`;
  html += `</div>`;
  return html;
}

export function errorCardHtml(title, detail) {
  return `<div class="card error-card"><div class="card-title">⚠️ ${esc(title)}</div><div>${esc(detail || "未知错误")}</div></div>`;
}

/* ---------- 快捷说明卡片 ---------- */

export function guideHtml() {
  return `<div>🎯 三步生成广告：</div>
    <div class="card">
      <div class="card-title">使用引导</div>
      <div style="font-size:13.5px;line-height:2">
        1️⃣ 点击输入框左侧 <b>＋</b> 上传产品图（jpg/png/webp ≤ ${maxFileMb()}MB），可附补充说明（Shift+Enter 换行）<br/>
        2️⃣ 核对 AI 识别出的产品信息，可编辑卖点、价格、促销<br/>
        3️⃣ 选择国家 / 语言 / 平台 / 风格 / 尺寸 / 文案版本数，一键生成广告文案 + 广告图
      </div>
    </div>`;
}

export function paramsInfoHtml() {
  return `<div>🌍 支持的投放参数：</div>
    <div class="card">
      <div class="card-title">参数一览</div>
      <div style="font-size:13.5px;line-height:2">
        <b>国家</b>：${Object.values(COUNTRIES).join(" / ")}<br/>
        <b>语言</b>：${Object.values(LANGUAGE_NAMES).join(" / ")}<br/>
        <b>平台</b>：${PLATFORMS.join(" / ")}<br/>
        <b>风格</b>：${Object.values(STYLES).join(" / ")}<br/>
        <b>尺寸</b>：${Object.values(SIZES).join(" / ")}<br/>
        <b>文案版本数</b>：1 / 2 / 3 版
      </div>
    </div>`;
}

/* ---------- 图片加载失败兜底 ---------- */

/** 图片加载失败兜底：原地替换为占位块（网络中断 / 签名过期 / 对象已删除都走这里） */
export function installImageFallback() {
  document.addEventListener(
    "error",
    (e) => {
      const el = e.target;
      if (!(el instanceof HTMLImageElement)) return;
      if (!el.dataset.fallback || el.dataset.failed) return;
      el.dataset.failed = "1";
      el.replaceWith(Object.assign(document.createElement("div"), {
        className: "gallery-ph",
        textContent: "🖼 图片加载失败",
        role: "img",
        ariaLabel: "广告图加载失败",
      }));
    },
    true // 捕获阶段：img 的 error 事件不冒泡
  );
}
